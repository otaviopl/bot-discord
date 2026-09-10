"""403 não é expiração de token.

Em Development Mode, uma conta que não está em User Management completa o OAuth
normalmente e só é barrada nas chamadas de API, com 403 "User not registered in the
Developer Dashboard". Tratar isso como token expirado mandava a pessoa rodar
`!conectar` de novo — o que reautoriza com sucesso e leva ao mesmo 403, em loop.
"""

import httpx
import pytest

from bot.spotify_client import SpotifyAuthError, SpotifyClient, SpotifyForbidden

# `env` é a fixture compartilhada com os testes do listener — importada de propósito.
from tests.test_spotify_listener import (  # noqa: F401
    USER_A, USER_B, conectar, env, mensagem, track_payload,
)

NAO_CADASTRADA = {
    "error": {"status": 403, "message": "User not registered in the Developer Dashboard"}
}
OUTRO_403 = {"error": {"status": 403, "message": "Insufficient client scope"}}


def client_with(handler) -> SpotifyClient:
    return SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class TestClassificacaoDoErro:
    async def test_403_nao_e_erro_de_autorizacao(self):
        api = client_with(lambda req: httpx.Response(403, json=NAO_CADASTRADA))

        with pytest.raises(SpotifyForbidden) as exc:
            await api.currently_playing("token")

        assert exc.value.conta_nao_cadastrada is True
        assert "not registered" in exc.value.motivo.lower()

    async def test_401_continua_sendo_erro_de_autorizacao(self):
        api = client_with(lambda req: httpx.Response(401, text="token expired"))

        with pytest.raises(SpotifyAuthError):
            await api.currently_playing("token")

    async def test_403_de_outro_motivo_nao_vira_conta_nao_cadastrada(self):
        api = client_with(lambda req: httpx.Response(403, json=OUTRO_403))

        with pytest.raises(SpotifyForbidden) as exc:
            await api.currently_playing("token")

        assert exc.value.conta_nao_cadastrada is False
        assert exc.value.motivo == "Insufficient client scope"

    async def test_403_sem_corpo_json_nao_quebra(self):
        api = client_with(lambda req: httpx.Response(403, text="Forbidden"))

        with pytest.raises(SpotifyForbidden) as exc:
            await api.currently_playing("token")

        assert "Forbidden" in exc.value.motivo


class TestNaoEntraEmLoop:
    """O caso que aconteceu de verdade: conecta, dá 403, e o bot mandava reconectar."""

    async def test_top_com_403_nao_marca_reconexao(self, env):
        await conectar(env["store"], USER_A, "Gi")
        env["respostas"]["top"] = httpx.Response(403, json=NAO_CADASTRADA)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        conta = await env["store"].get_account(USER_A)
        assert conta.needs_reauth is False, "403 não pode marcar a conta para reconexão"

    async def test_top_com_403_explica_o_cadastro_em_vez_de_pedir_reconexao(self, env):
        await conectar(env["store"], USER_A, "Gi")
        env["respostas"]["top"] = httpx.Response(403, json=NAO_CADASTRADA)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        embed = env["canal"].sent[0]
        assert "não liberada" in embed.title
        assert "User Management" in embed.description
        assert "Autorização expirada" not in embed.title
        assert "não resolve" in embed.description  # avisa que reconectar não adianta

    async def test_painel_com_403_nao_marca_reconexao(self, env):
        await conectar(env["store"], USER_A, "Gi")
        env["respostas"]["currently_playing"] = httpx.Response(403, json=NAO_CADASTRADA)

        await env["listener"].refresh_panel()

        conta = await env["store"].get_account(USER_A)
        assert conta.needs_reauth is False
        assert "não liberada" in env["canal"].sent[0].fields[0].value

    async def test_coleta_com_403_nao_marca_reconexao(self, env):
        await conectar(env["store"], USER_A, "Gi")
        env["respostas"]["recently_played"] = httpx.Response(403, json=NAO_CADASTRADA)

        await env["listener"].sync_recent_plays()

        conta = await env["store"].get_account(USER_A)
        assert conta.needs_reauth is False

    async def test_401_ainda_marca_reconexao(self, env):
        """A regressão inversa: token realmente expirado continua pedindo !conectar."""
        await conectar(env["store"], USER_A, "Gi")
        env["respostas"]["currently_playing"] = httpx.Response(401, text="expired")

        await env["listener"].refresh_panel()

        conta = await env["store"].get_account(USER_A)
        assert conta.needs_reauth is True
        assert "!conectar" in env["canal"].sent[0].fields[0].value

    async def test_403_de_uma_conta_nao_afeta_a_outra(self, env):
        """A conta do Otávio funciona; só a da Gi está barrada."""
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Gi")

        def handler(request):
            # o token é o mesmo nos dublês, então alterna pela ordem das chamadas
            return httpx.Response(200, json=track_payload())

        env["respostas"]["currently_playing"] = handler

        await env["listener"].refresh_panel()

        assert (await env["store"].get_account(USER_A)).needs_reauth is False
        assert (await env["store"].get_account(USER_B)).needs_reauth is False

    async def test_403_generico_mostra_o_motivo_do_spotify(self, env):
        await conectar(env["store"], USER_A, "Gi")
        env["respostas"]["top"] = httpx.Response(403, json=OUTRO_403)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        embed = env["canal"].sent[0]
        assert "Insufficient client scope" in embed.description
        assert "User Management" not in embed.description


class TestRecuperacao:
    async def test_conectar_de_novo_limpa_a_marca_antiga(self, env):
        """Quem já ficou marcado pelo bug volta ao normal com um !conectar."""
        await conectar(env["store"], USER_A, "Gi")
        await env["store"].mark_needs_reauth(USER_A)
        assert (await env["store"].get_account(USER_A)).needs_reauth is True

        # o callback do OAuth regrava a conta
        await conectar(env["store"], USER_A, "Gi")

        assert (await env["store"].get_account(USER_A)).needs_reauth is False
