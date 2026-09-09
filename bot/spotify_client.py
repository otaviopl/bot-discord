"""Cliente da Spotify Web API: reproducao atual, historico recente e rankings."""

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

API_BASE = "https://api.spotify.com/v1"

TIME_RANGES = {
    "short_term": "aproximadamente 4 semanas",
    "medium_term": "aproximadamente 6 meses",
    "long_term": "aproximadamente 1 ano",
}


class SpotifyAuthError(Exception):
    """Token invalido/revogado: exige reconexao pelo usuario."""


class SpotifyRateLimited(Exception):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


class SpotifyUnavailable(Exception):
    """Falha temporaria da API (5xx, timeout, rede)."""


class SpotifyClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._logger = logging.getLogger(__name__)

    async def _get(
        self,
        access_token: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 2,
    ) -> Optional[Dict[str, Any]]:
        """GET autenticado. Retorna None em 204 (sem conteudo)."""
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
            if response.status_code in (401, 403):
                raise SpotifyAuthError(f"{response.status_code}: {response.text[:200]}")
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "5"))
                self._logger.warning(
                    "Spotify rate limit atingido",
                    extra={"context": {"path": path, "retry_after": retry_after}},
                )
                raise SpotifyRateLimited(retry_after)
            if response.status_code >= 500:
                if attempt >= max_retries:
                    raise SpotifyUnavailable(f"{response.status_code}: {response.text[:200]}")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            raise SpotifyUnavailable(f"{response.status_code}: {response.text[:200]}")

        raise SpotifyUnavailable("esgotou as tentativas")

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
