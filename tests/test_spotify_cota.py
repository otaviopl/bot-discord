"""Cota e bloqueio do Spotify.

Em Development Mode o 429 vale para o app inteiro e, desde jul/2026, o estouro de
cota por conta de desenvolvedor devolve Retry-After de horas (visto em produção:
33715s). Três coisas não podem acontecer: gastar requisição durante o bloqueio,
esquecer o bloqueio num restart, e consultar a cada minuto quem nem está ouvindo.
"""

import time

import httpx

from bot.spotify_client import RateLimitGuard, SpotifyClient, SpotifyRateLimited
from bot.spotify_listener import (
    POLL_NEGADO_S,
    POLL_PARADO_S,
    RATE_LIMIT_KEY,
    SpotifyListener,
)

# `env` é a fixture compartilhada com os testes do listener — importada de propósito.
from tests.test_spotify_listener import (  # noqa: F401
    CHANNEL_ID, GUILD_ID, USER_A, USER_B, conectar, env, mensagem,
    track_payload,
)

COTA = {"error": {"status": 429, "message": "Too many requests"}, "reason": "QUOTA_EXCEEDED"}


def contador(resposta):
    chamadas = []

    def handler(request):
        chamadas.append(str(request.url.path))
        return resposta

    return handler, chamadas


class TestGuard:
    async def test_429_bloqueia_as_chamadas_seguintes_sem_requisicao(self):
        chamadas = []

        def handler(request):
            chamadas.append(1)
            return httpx.Response(429, headers={"Retry-After": "33715"}, json=COTA)

        api = SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        for _ in range(5):
            try:
                await api.currently_playing("token")
            except SpotifyRateLimited:
                pass

        assert len(chamadas) == 1, "depois do 429 nenhuma requisição pode sair"

    async def test_reconhece_cota_esgotada(self):
        api = SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "33715"}, json=COTA)
        )))

        try:
            await api.currently_playing("token")
        except SpotifyRateLimited as exc:
            assert exc.cota_esgotada is True
            assert exc.retry_after == 33715
            assert exc.local is False
        else:
            raise AssertionError("deveria ter levantado")

    async def test_bloqueio_local_e_marcado_como_local(self):
        api = SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "600"})
        )))
        try:
            await api.currently_playing("token")
        except SpotifyRateLimited:
            pass

        try:
            await api.currently_playing("token")
        except SpotifyRateLimited as exc:
            assert exc.local is True
            assert 0 < exc.retry_after <= 600

    async def test_retry_after_invalido_nao_quebra(self):
        api = SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "amanha"})
        )))
        try:
            await api.currently_playing("token")
        except SpotifyRateLimited as exc:
            assert exc.retry_after == 5.0

    async def test_bloqueio_menor_nao_encurta_um_maior(self):
        guard = RateLimitGuard()
        await guard.block(3600, "QUOTA_EXCEEDED")
        await guard.block(10, None)

        assert guard.remaining() > 3500
        assert guard.reason == "QUOTA_EXCEEDED"

    async def test_bloqueio_expira(self):
        guard = RateLimitGuard()
        guard.restore(time.time() - 1, None)
        assert guard.blocked() is False


class TestPersistencia:
    async def test_bloqueio_sobrevive_ao_restart(self, env):
        """Um restart (todo deploy) não pode esquecer o bloqueio e voltar a bater."""
        await conectar(env["store"], USER_A)
        await env["listener"]._restaurar_bloqueio()
        env["respostas"]["currently_playing"] = httpx.Response(
            429, headers={"Retry-After": "33715"}, json=COTA
        )
        await env["listener"].refresh_panel()

        assert await env["store"].get_value(RATE_LIMIT_KEY) is not None

        # processo novo, mesmo banco
        handler, chamadas = contador(httpx.Response(200, json=track_payload()))
        novo = SpotifyListener(
            store=env["store"],
            auth=env["auth"],
            client=SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(handler))),
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            allowed_user_ids=(USER_A, USER_B),
            oauth_host="127.0.0.1",
            oauth_port=0,
        )
        novo._discord = env["discord"]
        await novo._restaurar_bloqueio()

        assert novo._api.guard.blocked()
        assert novo._api.guard.reason == "QUOTA_EXCEEDED"

        await novo.refresh_panel()
        await novo.sync_recent_plays()
        assert chamadas == [], "o processo novo não pode chamar a API durante o bloqueio"

    async def test_bloqueio_vencido_nao_e_restaurado(self, env):
        await env["store"].set_value(RATE_LIMIT_KEY, str(time.time() - 60))
        await env["listener"]._restaurar_bloqueio()

        assert env["listener"]._api.guard.blocked() is False


class TestComandosDuranteOBloqueio:
    async def test_top_nao_chama_a_api_e_diz_quando_volta(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["listener"]._api.guard.block(33715, "QUOTA_EXCEEDED")

        handler, chamadas = contador(httpx.Response(200, json={"items": []}))
        env["respostas"]["top"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        embed = env["canal"].sent[0]
        assert chamadas == []
        assert "Cota do Spotify esgotada" in embed.description
        assert "33715" not in embed.description  # nada de segundos crus
        assert "9h22" in embed.description
        assert "continuam funcionando" in embed.description

    async def test_top_em_bloqueio_serve_o_ranking_guardado(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        env["respostas"]["top"] = httpx.Response(
            200, json={"items": [{"name": "Cinema", "artists": [{"name": "Harry"}]}]}
        )
        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        # cache de 6h: expira à força para testar o fallback
        chave = next(iter(env["listener"]._top_cache))
        _, faixas, artistas = env["listener"]._top_cache[chave]
        env["listener"]._top_cache[chave] = (0, faixas, artistas)
        await env["listener"]._api.guard.block(33715, "QUOTA_EXCEEDED")

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        embed = env["canal"].sent[1]
        assert "Cinema" in embed.fields[0].value
        assert "último ranking" in embed.footer.text

    async def test_bloqueio_curto_tem_mensagem_curta(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["listener"]._api.guard.block(90, None)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        assert "Muitas consultas" in env["canal"].sent[0].title

    async def test_minutos_funciona_durante_o_bloqueio(self, env):
        """Comandos que só leem o banco não dependem do Spotify."""
        await conectar(env["store"], USER_A, "Otávio")
        await env["listener"]._api.guard.block(33715, "QUOTA_EXCEEDED")

        await env["listener"].handle_message(env["discord"], mensagem(env, "!minutos"))

        assert "Tempo ouvido" in env["canal"].sent[0].title


class TestPainelDuranteOBloqueio:
    async def test_painel_avisa_a_pausa_uma_vez(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())
        await env["listener"].refresh_panel()
        painel = list(env["canal"].messages.values())[0]
        edicoes_antes = painel.edits

        await env["listener"]._api.guard.block(33715, "QUOTA_EXCEEDED")
        await env["listener"].refresh_panel()
        await env["listener"].refresh_panel()
        await env["listener"].refresh_panel()

        assert painel.edits == edicoes_antes + 1, "avisa uma vez, não a cada tick"
        assert "em pausa" in painel.embed.footer.text


class TestPollingAdaptativo:
    async def test_quem_esta_parado_nao_e_consultado_a_cada_tick(self, env):
        await conectar(env["store"], USER_A)
        handler, chamadas = contador(httpx.Response(204))
        env["respostas"]["currently_playing"] = handler

        await env["listener"].refresh_panel()
        for _ in range(4):  # quatro ticks seguidos, dentro dos 5 min de folga
            env["listener"]._panel_fingerprint = None
            await env["listener"].refresh_panel()

        assert len(chamadas) == 1

    async def test_quem_esta_parado_volta_a_ser_consultado_depois_do_intervalo(self, env):
        await conectar(env["store"], USER_A)
        handler, chamadas = contador(httpx.Response(204))
        env["respostas"]["currently_playing"] = handler

        await env["listener"].refresh_panel()
        agenda = env["listener"]._next_poll[USER_A]
        assert agenda - time.time() > POLL_PARADO_S - 5

        env["listener"]._next_poll[USER_A] = time.time() - 1  # o intervalo passou
        await env["listener"].refresh_panel()

        assert len(chamadas) == 2

    async def test_quem_esta_tocando_e_consultado_a_cada_minuto(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())

        await env["listener"].refresh_panel()

        assert env["listener"]._next_poll[USER_A] - time.time() <= 60

    async def test_conta_com_403_fica_meia_hora_sem_ser_consultada(self, env):
        await conectar(env["store"], USER_A)
        handler, chamadas = contador(httpx.Response(
            403, json={"error": {"status": 403, "message": "User not registered in the Developer Dashboard"}}
        ))
        env["respostas"]["currently_playing"] = handler

        await env["listener"].refresh_panel()
        for _ in range(10):
            env["listener"]._panel_fingerprint = None
            await env["listener"].refresh_panel()

        assert len(chamadas) == 1
        assert env["listener"]._next_poll[USER_A] - time.time() > POLL_NEGADO_S - 5

    async def test_coleta_pula_conta_com_403_recente(self, env):
        await conectar(env["store"], USER_A)
        handler, chamadas = contador(httpx.Response(
            403, json={"error": {"status": 403, "message": "User not registered in the Developer Dashboard"}}
        ))
        env["respostas"]["recently_played"] = handler

        await env["listener"].sync_recent_plays()
        await env["listener"].sync_recent_plays()
        await env["listener"].sync_recent_plays()

        assert len(chamadas) == 1

    async def test_agora_reaproveita_leitura_recente(self, env):
        await conectar(env["store"], USER_A)
        handler, chamadas = contador(httpx.Response(200, json=track_payload()))
        env["respostas"]["currently_playing"] = handler

        await env["listener"].refresh_panel()               # lê agora
        await env["listener"].handle_message(env["discord"], mensagem(env, "!agora"))

        assert len(chamadas) == 1, "leitura de segundos atrás não precisa ser refeita"

    async def test_agora_consulta_se_a_leitura_for_velha(self, env):
        await conectar(env["store"], USER_A)
        handler, chamadas = contador(httpx.Response(204))
        env["respostas"]["currently_playing"] = handler

        await env["listener"].refresh_panel()
        env["listener"]._polled_at[USER_A] = time.time() - 120  # leitura de 2 min atrás

        await env["listener"].handle_message(env["discord"], mensagem(env, "!agora"))

        assert len(chamadas) == 2


class TestCacheDoTop:
    async def test_segundo_top_sai_do_cache(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        handler, chamadas = contador(httpx.Response(200, json={"items": []}))
        env["respostas"]["top"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))
        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        assert len(chamadas) == 2  # faixas + artistas da primeira vez; a segunda é cache

    async def test_periodos_diferentes_nao_dividem_cache(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        handler, chamadas = contador(httpx.Response(200, json={"items": []}))
        env["respostas"]["top"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top 4-semanas"))
        await env["listener"].handle_message(env["discord"], mensagem(env, "!top 1-ano"))

        assert len(chamadas) == 4
