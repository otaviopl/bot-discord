"""Cliente da Spotify Web API: reproducao atual, historico recente e rankings."""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

API_BASE = "https://api.spotify.com/v1"

TIME_RANGES = {
    "short_term": "aproximadamente 4 semanas",
    "medium_term": "aproximadamente 6 meses",
    "long_term": "aproximadamente 1 ano",
}


class SpotifyAuthError(Exception):
    """401: token invalido ou expirado. Reconectar resolve."""


class SpotifyForbidden(Exception):
    """403: o token e valido, mas a conta nao tem permissao para esta chamada.

    Reconectar NAO resolve — e configuracao do app, nao da autorizacao. O caso
    comum e a conta nao estar cadastrada em User Management enquanto o app esta
    em Development Mode: o OAuth completa normalmente e so as chamadas de API
    sao barradas, o que faz o erro parecer expiracao de token.
    """

    def __init__(self, message: str, motivo: Optional[str] = None) -> None:
        super().__init__(message)
        self.motivo = motivo or message

    @property
    def conta_nao_cadastrada(self) -> bool:
        return "not registered" in self.motivo.lower()


class SpotifyRateLimited(Exception):
    """429. `local=True` quando foi o proprio guard que barrou, sem requisicao."""

    def __init__(
        self, retry_after: float, reason: Optional[str] = None, local: bool = False
    ) -> None:
        super().__init__(f"rate limited ({reason or 'sem motivo'}), retry after {retry_after}s")
        self.retry_after = retry_after
        self.reason = reason
        self.local = local

    @property
    def cota_esgotada(self) -> bool:
        return (self.reason or "").upper() == "QUOTA_EXCEEDED"


class RateLimitGuard:
    """Bloqueio compartilhado por todas as chamadas a API.

    Em Development Mode o 429 vale para o app inteiro, nao para a rota nem para o
    usuario — e desde jul/2026 existe tambem uma cota por conta de desenvolvedor,
    cujo estouro devolve Retry-After de horas. Enquanto o bloqueio vale, nenhuma
    chamada sai: insistir so gasta requisicao e, na pior hipotese, prolonga a pena.

    `on_change` persiste o bloqueio, para um restart nao esquecer e voltar a bater.
    """

    def __init__(self) -> None:
        self.until: float = 0.0
        self.reason: Optional[str] = None
        self.on_change: Optional[Callable[[float, Optional[str]], Awaitable[None]]] = None

    def remaining(self, now: Optional[float] = None) -> float:
        return max(0.0, self.until - (now if now is not None else time.time()))

    def blocked(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) > 0

    def restore(self, until: float, reason: Optional[str]) -> None:
        self.until = until
        self.reason = reason

    async def block(self, seconds: float, reason: Optional[str]) -> None:
        novo = time.time() + seconds
        if novo <= self.until:
            return
        self.until = novo
        self.reason = reason
        if self.on_change is not None:
            await self.on_change(self.until, self.reason)


class SpotifyUnavailable(Exception):
    """Falha temporaria da API (5xx, timeout, rede)."""


class SpotifyClient:
    def __init__(self, http: httpx.AsyncClient, guard: Optional[RateLimitGuard] = None) -> None:
        self._http = http
        self.guard = guard or RateLimitGuard()
        self._logger = logging.getLogger(__name__)

    async def _get(
        self,
        access_token: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 2,
    ) -> Optional[Dict[str, Any]]:
        """GET autenticado. Retorna None em 204 (sem conteudo)."""
        if self.guard.blocked():
            raise SpotifyRateLimited(self.guard.remaining(), self.guard.reason, local=True)

        url = f"{API_BASE}{path}"
        headers = {"Authorization": f"Bearer {access_token}"}

        for attempt in range(max_retries + 1):
            try:
                response = await self._http.get(url, headers=headers, params=params, timeout=15.0)
            except httpx.HTTPError as exc:
                if attempt >= max_retries:
                    raise SpotifyUnavailable(str(exc)) from exc
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if response.status_code == 204:
                return None
            if response.status_code == 200:
                if not response.content:
                    return None
                return response.json()
            if response.status_code == 401:
                raise SpotifyAuthError(f"401: {response.text[:200]}")
            if response.status_code == 403:
                motivo = self._extrair_motivo(response)
                self._logger.warning(
                    "Spotify recusou a chamada com 403",
                    extra={"context": {"path": path, "motivo": motivo}},
                )
                raise SpotifyForbidden(f"403: {motivo}", motivo)
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", "5"))
                except ValueError:
                    retry_after = 5.0
                reason = self._extrair_razao_429(response)
                self._logger.warning(
                    "Spotify bloqueou o app (429)",
                    extra={
                        "context": {
                            "path": path,
                            "retry_after": retry_after,
                            "reason": reason,
                        }
                    },
                )
                await self.guard.block(retry_after, reason)
                raise SpotifyRateLimited(retry_after, reason)
            if response.status_code >= 500:
                if attempt >= max_retries:
                    raise SpotifyUnavailable(f"{response.status_code}: {response.text[:200]}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            raise SpotifyUnavailable(f"{response.status_code}: {response.text[:200]}")

        raise SpotifyUnavailable("esgotou as tentativas")

    @staticmethod
    def _extrair_razao_429(response: httpx.Response) -> Optional[str]:
        """Desde jul/2026 o 429 de cota traz `reason: QUOTA_EXCEEDED` no corpo."""
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("reason"):
            return str(payload["reason"])
        erro = payload.get("error")
        if isinstance(erro, dict):
            return erro.get("reason") or erro.get("message")
        return None

    @staticmethod
    def _extrair_motivo(response: httpx.Response) -> str:
        """O corpo do erro do Spotify traz a razao exata em error.message."""
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(payload, dict):
            erro = payload.get("error")
            if isinstance(erro, dict) and erro.get("message"):
                return str(erro["message"])
            if isinstance(erro, str):
                return erro
        return response.text[:200]

    async def current_user(self, access_token: str) -> Dict[str, Any]:
        data = await self._get(access_token, "/me")
        return data or {}

    async def currently_playing(self, access_token: str) -> Optional[Dict[str, Any]]:
        """None quando nao ha reproducao ativa (API responde 204)."""
        return await self._get(
            access_token,
            "/me/player/currently-playing",
            params={"additional_types": "track"},
        )

    async def recently_played(
        self, access_token: str, after_ms: Optional[int] = None, limit: int = 50
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"limit": min(max(limit, 1), 50)}
        if after_ms:
            params["after"] = after_ms
        data = await self._get(access_token, "/me/player/recently-played", params=params)
        return data or {"items": [], "cursors": None}

    async def library_contains(
        self, access_token: str, track_ids: List[str]
    ) -> Dict[str, bool]:
        """Diz quais dessas faixas a pessoa tem salvas na biblioteca.

        O endpoint aceita `uris` (nao ids) e no maximo 40 por chamada, entao a lista
        e quebrada em lotes. Exige o escopo user-library-read.
        """
        resultado: Dict[str, bool] = {}

        for inicio in range(0, len(track_ids), 40):
            lote = track_ids[inicio : inicio + 40]
            uris = ",".join(f"spotify:track:{tid}" for tid in lote)
            data = await self._get(access_token, "/me/library/contains", params={"uris": uris})

            marcados = data if isinstance(data, list) else (data or {}).get("contains") or []
            for track_id, salvo in zip(lote, marcados):
                resultado[track_id] = bool(salvo)

        return resultado

    async def artist(self, access_token: str, artist_id: str) -> Optional[Dict[str, Any]]:
        """Dados do artista. O campo `genres` esta marcado como deprecated e volta
        vazio para muitos artistas — quem chama precisa lidar com isso."""
        return await self._get(access_token, f"/artists/{artist_id}")

    async def top_items(
        self, access_token: str, kind: str, time_range: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        if kind not in ("tracks", "artists"):
            raise ValueError("kind deve ser 'tracks' ou 'artists'")
        if time_range not in TIME_RANGES:
            raise ValueError(f"time_range invalido: {time_range}")

        data = await self._get(
            access_token,
            f"/me/top/{kind}",
            params={"time_range": time_range, "limit": min(max(limit, 1), 50)},
        )
        return (data or {}).get("items", [])
