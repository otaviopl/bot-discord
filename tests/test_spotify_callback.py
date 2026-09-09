"""Servidor de callback do OAuth, exercitado com requisicoes HTTP reais no loopback."""

import socket

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.spotify_auth import SpotifyAuth
from bot.spotify_store import SpotifyStore

USER_A = 111111111111111111


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def servidor(tmp_path):
    store = SpotifyStore(str(tmp_path / "spotify.db"), Fernet.generate_key().decode())
    conectados = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/token":
            assert "grant_type=authorization_code" in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 3600,
                    "scope": "user-read-currently-playing",
                },
            )
        if request.url.path == "/v1/me":
            return httpx.Response(200, json={"id": "sp-user", "display_name": "Otávio"})
        return httpx.Response(404)

    porta = porta_livre()
    auth = SpotifyAuth(
        client_id="cid",
        client_secret="secret",
        redirect_uri=f"http://127.0.0.1:{porta}/spotify/callback",
        store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    async def on_connected(user_id, display_name):
        conectados.append((user_id, display_name))

    await auth.start_callback_server("127.0.0.1", porta, on_connected)
    try:
        yield {"auth": auth, "store": store, "porta": porta, "conectados": conectados}
    finally:
        await auth.stop_callback_server()


async def get(porta: int, path: str) -> httpx.Response:
    async with httpx.AsyncClient() as client:
        return await client.get(f"http://127.0.0.1:{porta}{path}", timeout=10.0)


class TestCallback:
    async def test_fluxo_completo_conecta_a_conta(self, servidor):
        url = await servidor["auth"].build_auth_url(USER_A)
        state = url.split("state=")[1].split("&")[0]

        resposta = await get(servidor["porta"], f"/spotify/callback?code=abc&state={state}")

        assert resposta.status_code == 200
        assert "Spotify conectado" in resposta.text

        conta = await servidor["store"].get_account(USER_A)
        assert conta.access_token == "access-1"
        assert conta.refresh_token == "refresh-1"
        assert conta.spotify_user_id == "sp-user"
        assert conta.needs_reauth is False
        assert servidor["conectados"] == [(USER_A, "Otávio")]

    async def test_state_forjado_e_recusado(self, servidor):
        resposta = await get(servidor["porta"], "/spotify/callback?code=abc&state=inventado")

        assert resposta.status_code == 400
        assert await servidor["store"].get_account(USER_A) is None

    async def test_state_nao_pode_ser_reutilizado(self, servidor):
        url = await servidor["auth"].build_auth_url(USER_A)
        state = url.split("state=")[1].split("&")[0]

        primeira = await get(servidor["porta"], f"/spotify/callback?code=abc&state={state}")
        segunda = await get(servidor["porta"], f"/spotify/callback?code=abc&state={state}")

        assert primeira.status_code == 200
        assert segunda.status_code == 400

    async def test_usuario_que_nega_autorizacao(self, servidor):
        url = await servidor["auth"].build_auth_url(USER_A)
        state = url.split("state=")[1].split("&")[0]

        resposta = await get(
            servidor["porta"], f"/spotify/callback?error=access_denied&state={state}"
        )

        assert resposta.status_code == 400
        assert "negada" in resposta.text
        assert await servidor["store"].get_account(USER_A) is None

    async def test_callback_sem_code(self, servidor):
        url = await servidor["auth"].build_auth_url(USER_A)
        state = url.split("state=")[1].split("&")[0]

        resposta = await get(servidor["porta"], f"/spotify/callback?state={state}")
        assert resposta.status_code == 400

    async def test_rota_desconhecida_devolve_404(self, servidor):
        resposta = await get(servidor["porta"], "/qualquer-coisa")
        assert resposta.status_code == 404

    async def test_healthcheck_responde(self, servidor):
        resposta = await get(servidor["porta"], "/health")
        assert resposta.status_code == 200
        assert resposta.text == "ok"

    async def test_servidor_continua_de_pe_apos_requisicao_invalida(self, servidor):
        await get(servidor["porta"], "/spotify/callback?state=lixo")
        assert (await get(servidor["porta"], "/health")).status_code == 200

    async def test_reconectar_substitui_a_autorizacao_anterior(self, servidor):
        for _ in range(2):
            url = await servidor["auth"].build_auth_url(USER_A)
            state = url.split("state=")[1].split("&")[0]
            resposta = await get(servidor["porta"], f"/spotify/callback?code=abc&state={state}")
            assert resposta.status_code == 200

        contas = await servidor["store"].list_accounts()
        assert len(contas) == 1  # atualizou, não duplicou
