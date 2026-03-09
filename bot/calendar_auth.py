import asyncio
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TOKEN_FILE = "calendar_token.json"


class CalendarAuth:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._logger = logging.getLogger(__name__)

    def get_credentials(self) -> Optional[Credentials]:
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except (FileNotFoundError, ValueError):
            return None

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._save_credentials(creds)
            except Exception:
                return None

        return creds if creds.valid else None

    def is_authenticated(self) -> bool:
        return self.get_credentials() is not None

    def get_auth_url(self) -> str:
        flow = self._create_flow()
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        return auth_url

    async def wait_for_callback(self, port: int, timeout: float = 300.0) -> Optional[str]:
        """Sobe um servidor HTTP temporário e aguarda o code do callback OAuth."""
        code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
                request_line = data.decode(errors="ignore").split("\r\n")[0]
                parts = request_line.split(" ")
                if len(parts) >= 2:
                    parsed = urlparse(parts[1])
                    params = parse_qs(parsed.query)
                    if "code" in params and not code_future.done():
                        code_future.set_result(params["code"][0])
            except Exception:
                pass
            finally:
                body = b"<h1>Autenticacao concluida! Pode fechar esta aba.</h1>"
                response = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Connection: close\r\n\r\n" + body
                )
                writer.write(response)
                await writer.drain()
                writer.close()

        server = await asyncio.start_server(_handle, "0.0.0.0", port)
        self._logger.info(
            "OAuth callback server started",
            extra={"context": {"port": port}},
        )

        try:
            return await asyncio.wait_for(code_future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            server.close()
            await server.wait_closed()
            self._logger.info("OAuth callback server stopped")

    async def exchange_code(self, code: str) -> Credentials:
        flow = self._create_flow()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: flow.fetch_token(code=code))
        creds = flow.credentials
        self._save_credentials(creds)
        self._logger.info("OAuth credentials saved successfully")
        return creds

    def _create_flow(self) -> Flow:
        client_config = {
            "web": {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uris": [self._redirect_uri],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        return Flow.from_client_config(
            client_config,
            scopes=SCOPES,
            redirect_uri=self._redirect_uri,
        )

    def _save_credentials(self, creds: Credentials) -> None:
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
