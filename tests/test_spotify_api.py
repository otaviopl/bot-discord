"""Cliente da Web API e fluxo de OAuth: estados do player, erros e renovacao de token."""

import time

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.spotify_auth import SpotifyAuth
from bot.spotify_client import (
    SpotifyAuthError,
    SpotifyClient,
    SpotifyRateLimited,
    SpotifyUnavailable,
)
from bot.spotify_format import describe_playback
from bot.spotify_store import SpotifyStore

USER_A = 111111111111111111


def track_payload(name="Cinema", artist="Harry Styles", is_playing=True):
    return {
        "is_playing": is_playing,
        "currently_playing_type": "track",
        "progress_ms": 42_000,
        "item": {
            "id": "track-1",
            "name": name,
            "artists": [{"id": "art-1", "name": artist}],
            "album": {"name": "Album", "images": [{"url": "https://img/capa.jpg"}]},
            "external_urls": {"spotify": "https://open.spotify.com/track/track-1"},
        },
    }


def client_with(handler) -> SpotifyClient:
    return SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TestReproducaoAtual:
    async def test_musica_tocando(self):
        api = client_with(lambda req: httpx.Response(200, json=track_payload()))
        estado = describe_playback(await api.currently_playing("token"))

        assert estado["status"] == "playing"
        assert estado["text"] == "Tocando agora"
        assert estado["track_name"] == "Cinema"
        assert estado["artists"] == "Harry Styles"
        assert estado["image"] == "https://img/capa.jpg"

    async def test_musica_pausada(self):
        api = client_with(lambda req: httpx.Response(200, json=track_payload(is_playing=False)))
        estado = describe_playback(await api.currently_playing("token"))

        assert estado["status"] == "paused"
        assert estado["text"] == "Pausado"
        assert estado["track_name"] == "Cinema"

    async def test_sem_reproducao_ativa_responde_204(self):
        api = client_with(lambda req: httpx.Response(204))
        estado = describe_playback(await api.currently_playing("token"))

        assert estado["status"] == "idle"
        assert estado["text"] == "Sem reprodução ativa"

    async def test_anuncio_nao_vira_musica(self):
        payload = {"is_playing": True, "currently_playing_type": "ad", "item": None}
        api = client_with(lambda req: httpx.Response(200, json=payload))
        estado = describe_playback(await api.currently_playing("token"))

        assert estado["status"] == "ad"
        assert "track_name" not in estado

    async def test_podcast_nao_vira_musica(self):
        payload = {"is_playing": True, "currently_playing_type": "episode", "item": {"name": "Ep"}}
        estado = describe_playback(payload)

        assert estado["status"] == "unsupported"


class TestErrosDaApi:
    async def test_token_invalido_vira_erro_de_autorizacao(self):
        api = client_with(lambda req: httpx.Response(401, text="expired token"))
        with pytest.raises(SpotifyAuthError):
            await api.currently_playing("token")

    async def test_rate_limit_expoe_retry_after(self):
        api = client_with(
            lambda req: httpx.Response(429, headers={"Retry-After": "12"}, text="slow down")
        )
        with pytest.raises(SpotifyRateLimited) as exc:
            await api.currently_playing("token")

        assert exc.value.retry_after == 12.0

    async def test_spotify_fora_do_ar_tenta_de_novo_e_desiste(self):
        chamadas = []

        def handler(request):
            chamadas.append(request.url.path)
            return httpx.Response(503, text="unavailable")

        api = client_with(handler)
        with pytest.raises(SpotifyUnavailable):
            await api.currently_playing("token")

        assert len(chamadas) == 3  # tentativa inicial + 2 retries

    async def test_erro_de_rede_vira_indisponibilidade(self):
        def handler(request):
            raise httpx.ConnectError("sem rede")

        api = client_with(handler)
        with pytest.raises(SpotifyUnavailable):
            await api.currently_playing("token")

    async def test_falha_temporaria_se_recupera_no_retry(self):
        respostas = [httpx.Response(500), httpx.Response(200, json=track_payload())]
        api = client_with(lambda req: respostas.pop(0))

        estado = describe_playback(await api.currently_playing("token"))
        assert estado["status"] == "playing"


class TestRankings:
    async def test_top_pede_o_periodo_certo(self):
        capturado = {}

        def handler(request):
            capturado["url"] = str(request.url)
            return httpx.Response(200, json={"items": [{"name": "Faixa"}]})

        api = client_with(handler)
        itens = await api.top_items("token", "tracks", "medium_term", limit=10)

        assert itens == [{"name": "Faixa"}]
        assert "time_range=medium_term" in capturado["url"]
        assert "limit=10" in capturado["url"]

    async def test_periodo_invalido_e_recusado(self):
        api = client_with(lambda req: httpx.Response(200, json={"items": []}))
        with pytest.raises(ValueError):
            await api.top_items("token", "tracks", "decada", limit=10)


class TestHistoricoRecente:
    async def test_cursor_after_e_enviado(self):
        capturado = {}

        def handler(request):
            capturado["url"] = str(request.url)
            return httpx.Response(200, json={"items": [], "cursors": None})

        api = client_with(handler)
        await api.recently_played("token", after_ms=1_700_000_000_000)

        assert "after=1700000000000" in capturado["url"]
        assert "limit=50" in capturado["url"]


class TestOAuth:
    @pytest.fixture
    def store(self, tmp_path):
        return SpotifyStore(str(tmp_path / "spotify.db"), Fernet.generate_key().decode())

    def auth_with(self, store, handler):
        return SpotifyAuth(
            client_id="cid",
            client_secret="secret",
            redirect_uri="https://exemplo.com/spotify/callback",
            store=store,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    async def test_link_de_autorizacao_tem_state_e_escopos_minimos(self, store):
        auth = self.auth_with(store, lambda req: httpx.Response(200))
        url = await auth.build_auth_url(USER_A)

        assert "https://accounts.spotify.com/authorize?" in url
        assert "state=" in url
        assert "user-read-currently-playing" in url
        assert "user-read-recently-played" in url
        assert "user-top-read" in url
        # nada de controle de reproducao nem playlists
        assert "user-modify-playback-state" not in url
        assert "playlist" not in url

    async def test_cada_pessoa_recebe_um_state_proprio(self, store):
        auth = self.auth_with(store, lambda req: httpx.Response(200))
        url_a = await auth.build_auth_url(USER_A)
        url_b = await auth.build_auth_url(222222222222222222)

        state_a = url_a.split("state=")[1].split("&")[0]
        state_b = url_b.split("state=")[1].split("&")[0]
        assert state_a != state_b

    async def test_token_valido_nao_dispara_refresh(self, store):
        chamadas = []

        def handler(request):
            chamadas.append(request.url.path)
            return httpx.Response(200, json={})

        auth = self.auth_with(store, handler)
        await store.save_account(
            USER_A, "sp", "Nome", "access-atual", "refresh", int(time.time()) + 3600,
            "scope", int(time.time()),
        )
        conta = await store.get_account(USER_A)

        assert await auth.get_valid_access_token(conta) == "access-atual"
        assert chamadas == []

    async def test_token_expirado_e_renovado_e_persistido(self, store):
        def handler(request):
            assert request.url.path == "/api/token"
            body = request.content.decode()
            assert "grant_type=refresh_token" in body
            return httpx.Response(
                200, json={"access_token": "access-novo", "expires_in": 3600}
            )

        auth = self.auth_with(store, handler)
        await store.save_account(
            USER_A, "sp", "Nome", "access-velho", "refresh", int(time.time()) - 10,
            "scope", int(time.time()),
        )
        conta = await store.get_account(USER_A)

        assert await auth.get_valid_access_token(conta) == "access-novo"

        salvo = await store.get_account(USER_A)
        assert salvo.access_token == "access-novo"
        assert salvo.refresh_token == "refresh"  # preservado
        assert salvo.expires_at > int(time.time())

    async def test_refresh_token_rotacionado_e_salvo(self, store):
        auth = self.auth_with(
            store,
            lambda req: httpx.Response(
                200,
                json={
                    "access_token": "access-novo",
                    "refresh_token": "refresh-novo",
                    "expires_in": 3600,
                },
            ),
        )
        await store.save_account(
            USER_A, "sp", "Nome", "a", "refresh-antigo", 0, "scope", int(time.time())
        )

        await auth.get_valid_access_token(await store.get_account(USER_A))

        assert (await store.get_account(USER_A)).refresh_token == "refresh-novo"

    async def test_autorizacao_revogada_pede_reconexao(self, store):
        auth = self.auth_with(
            store, lambda req: httpx.Response(400, json={"error": "invalid_grant"})
        )
        await store.save_account(
            USER_A, "sp", "Nome", "a", "refresh-revogado", 0, "scope", int(time.time())
        )

        with pytest.raises(SpotifyAuthError):
            await auth.get_valid_access_token(await store.get_account(USER_A))

        assert (await store.get_account(USER_A)).needs_reauth is True
