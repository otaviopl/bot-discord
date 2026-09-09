"""OAuth do Spotify por usuario: autorizacao individual, callback HTTPS e refresh automatico."""

import asyncio
import base64
import logging
import secrets
import time
from typing import Awaitable, Callable, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .spotify_client import SpotifyAuthError
from .spotify_store import SpotifyAccount, SpotifyStore

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# Somente o necessario: reproducao atual, historico recente e rankings.
SCOPES = ("user-read-currently-playing", "user-read-recently-played", "user-top-read")

STATE_MAX_AGE_SECONDS = 900  # 15 minutos para concluir a autorizacao
TOKEN_REFRESH_MARGIN_SECONDS = 60

_HTML_OK = (
    "<!doctype html><meta charset='utf-8'>"
    "<title>Spotify conectado</title>"
    "<div style=\"font-family:system-ui;max-width:32rem;margin:4rem auto;text-align:center\">"
    "<h1>Spotify conectado</h1>"
    "<p>Pode fechar esta aba e voltar para o Discord.</p></div>"
)
_HTML_ERROR = (
    "<!doctype html><meta charset='utf-8'>"
    "<title>Falha ao conectar</title>"
    "<div style=\"font-family:system-ui;max-width:32rem;margin:4rem auto;text-align:center\">"
    "<h1>Nao deu certo</h1><p>{reason}</p>"
    "<p>Volte ao Discord e use <code>!conectar</code> de novo.</p></div>"
)


class SpotifyAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        store: SpotifyStore,
        http: httpx.AsyncClient,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._store = store
        self._http = http
        self._logger = logging.getLogger(__name__)
        self._server: Optional[asyncio.AbstractServer] = None
        self._refresh_locks: dict[int, asyncio.Lock] = {}
        self._on_connected: Optional[Callable[[int, Optional[str]], Awaitable[None]]] = None

    @property
    def callback_path(self) -> str:
        return urlparse(self._redirect_uri).path or "/spotify/callback"

    # ------------------------------------------------------------------ #
    # URL de autorizacao
    # ------------------------------------------------------------------ #

    async def build_auth_url(self, discord_user_id: int) -> str:
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        await self._store.purge_expired_states(STATE_MAX_AGE_SECONDS, now)
        await self._store.save_oauth_state(state, discord_user_id, now)

        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "show_dialog": "true",
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    # ------------------------------------------------------------------ #
    # Servidor de callback (persistente)
    # ------------------------------------------------------------------ #

    async def start_callback_server(
        self,
        host: str,
        port: int,
        on_connected: Callable[[int, Optional[str]], Awaitable[None]],
    ) -> None:
        if self._server is not None:
            return
        self._on_connected = on_connected
        self._server = await asyncio.start_server(self._handle_request, host, port)
        self._logger.info(
            "Spotify OAuth callback server started",
            extra={"context": {"host": host, "port": port, "path": self.callback_path}},
        )

    async def stop_callback_server(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        self._logger.info("Spotify OAuth callback server stopped")

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return

        try:
            request_line = head.decode(errors="ignore").split("\r\n", 1)[0]
            parts = request_line.split(" ")
            if len(parts) < 2:
                await self._respond(writer, 400, "<h1>Bad request</h1>")
                return

            method, target = parts[0], parts[1]
            parsed = urlparse(target)

            if parsed.path == "/health":
                await self._respond(writer, 200, "ok", content_type="text/plain; charset=utf-8")
                return

            if method != "GET" or parsed.path != self.callback_path:
                await self._respond(writer, 404, "<h1>Not found</h1>")
                return

            params = parse_qs(parsed.query)
            status, body = await self._process_callback(params)
            await self._respond(writer, status, body)
        except Exception as exc:  # nunca derruba o servidor por causa de uma requisicao
            self._logger.error(
                "Erro ao tratar callback do Spotify",
                extra={"context": {"error": str(exc)}},
            )
            try:
                await self._respond(writer, 500, _HTML_ERROR.format(reason="Erro interno."))
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _process_callback(self, params: dict) -> Tuple[int, str]:
        error = params.get("error", [None])[0]
        state = params.get("state", [None])[0]
        code = params.get("code", [None])[0]

        if error:
            self._logger.warning(
                "Usuario negou autorizacao do Spotify",
                extra={"context": {"error": error}},
            )
            return 400, _HTML_ERROR.format(reason="A autorizacao foi negada.")

        if not state or not code:
            return 400, _HTML_ERROR.format(reason="Link incompleto ou invalido.")

        discord_user_id = await self._store.consume_oauth_state(
            state, STATE_MAX_AGE_SECONDS, int(time.time())
        )
        if discord_user_id is None:
            self._logger.warning("State de OAuth invalido ou expirado")
            return 400, _HTML_ERROR.format(reason="Link expirado ou ja usado.")

        try:
            tokens = await self._exchange_code(code)
        except Exception as exc:
            self._logger.error(
                "Falha ao trocar code por token",
                extra={"context": {"error": str(exc), "discord_user_id": str(discord_user_id)}},
            )
            return 502, _HTML_ERROR.format(reason="O Spotify recusou a troca de credenciais.")

        display_name: Optional[str] = None
        spotify_user_id: Optional[str] = None
        try:
            from .spotify_client import SpotifyClient

            profile = await SpotifyClient(self._http).current_user(tokens["access_token"])
            display_name = profile.get("display_name")
            spotify_user_id = profile.get("id")
        except Exception as exc:
            self._logger.warning(
                "Nao foi possivel ler o perfil do Spotify apos conectar",
                extra={"context": {"error": str(exc)}},
            )

        now = int(time.time())
        await self._store.save_account(
            discord_user_id=discord_user_id,
            spotify_user_id=spotify_user_id,
            display_name=display_name,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            expires_at=now + int(tokens.get("expires_in", 3600)),
            scope=tokens.get("scope"),
            connected_at=now,
        )
        self._logger.info(
            "Conta do Spotify conectada",
            extra={"context": {"discord_user_id": str(discord_user_id), "spotify_user_id": spotify_user_id}},
        )

        if self._on_connected is not None:
            try:
                await self._on_connected(discord_user_id, display_name)
            except Exception as exc:
                self._logger.warning(
                    "Falha ao notificar conexao no Discord",
                    extra={"context": {"error": str(exc)}},
                )

        return 200, _HTML_OK

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        body: str,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        reasons = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error", 502: "Bad Gateway"}
        payload = body.encode("utf-8")
        header = (
            f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(header + payload)
        await writer.drain()

    # ------------------------------------------------------------------ #
    # Tokens
    # ------------------------------------------------------------------ #

    def _basic_auth_header(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    async def _exchange_code(self, code: str) -> dict:
        response = await self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
            },
            headers={
                "Authorization": self._basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15.0,
        )
        response.raise_for_status()
        payload = response.json()
        if "refresh_token" not in payload:
            raise ValueError("Spotify nao devolveu refresh_token")
        return payload

    async def get_valid_access_token(self, account: SpotifyAccount) -> str:
        """Devolve um access token valido, renovando quando necessario."""
        if account.expires_at - TOKEN_REFRESH_MARGIN_SECONDS > int(time.time()):
            return account.access_token

        lock = self._refresh_locks.setdefault(account.discord_user_id, asyncio.Lock())
        async with lock:
            fresh = await self._store.get_account(account.discord_user_id)
            if fresh is None:
                raise SpotifyAuthError("conta desconectada durante o refresh")
            if fresh.expires_at - TOKEN_REFRESH_MARGIN_SECONDS > int(time.time()):
                return fresh.access_token
            return await self._refresh(fresh)

    async def _refresh(self, account: SpotifyAccount) -> str:
        self._logger.info(
            "Renovando access token do Spotify",
            extra={"context": {"discord_user_id": str(account.discord_user_id)}},
        )
        try:
            response = await self._http.post(
                TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": account.refresh_token},
                headers={
                    "Authorization": self._basic_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise SpotifyAuthError(f"falha de rede no refresh: {exc}") from exc

        if response.status_code in (400, 401):
            await self._store.mark_needs_reauth(account.discord_user_id)
            self._logger.warning(
                "Refresh token invalido: usuario precisa reconectar",
                extra={"context": {"discord_user_id": str(account.discord_user_id)}},
            )
            raise SpotifyAuthError("refresh token revogado")

        response.raise_for_status()
        payload = response.json()
        access_token = payload["access_token"]
        expires_at = int(time.time()) + int(payload.get("expires_in", 3600))
        await self._store.update_tokens(
            account.discord_user_id,
            access_token=access_token,
            expires_at=expires_at,
            refresh_token=payload.get("refresh_token"),
        )
        return access_token
