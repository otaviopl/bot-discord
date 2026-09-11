"""Comandos, painel, coleta e resumo semanal — inclusive apos reinicio do servico."""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.spotify_auth import SpotifyAuth
from bot.spotify_client import SpotifyClient
from bot.spotify_format import (
    format_day_range,
    iso_week_key,
    last_7_days,
    parse_period,
    parse_week_offset,
    week_bounds,
)
from bot.spotify_listener import SpotifyListener
from bot.spotify_store import SpotifyStore
from tests.conftest import FakeChannel, FakeDiscordClient, FakeGuild, FakeMessage, FakeUser

BRT = ZoneInfo("America/Sao_Paulo")
GUILD_ID = 900000000000000001
CHANNEL_ID = 900000000000000002
OUTRO_CANAL = 900000000000000003
USER_A = 111111111111111111
USER_B = 222222222222222222
INTRUSO = 333333333333333333


def track_payload(track_id="track-1", name="Cinema", artist="Harry Styles", is_playing=True):
    return {
        "is_playing": is_playing,
        "currently_playing_type": "track",
        "item": {
            "id": track_id,
            "name": name,
            "artists": [{"id": "art-1", "name": artist}],
            "album": {"name": "Album", "images": [{"url": "https://img/capa.jpg"}]},
            "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
        },
    }


@pytest.fixture
def env(tmp_path):
    """Monta listener + dublês do Discord com respostas do Spotify programáveis."""
    store = SpotifyStore(str(tmp_path / "spotify.db"), Fernet.generate_key().decode())
    respostas = {
        "currently_playing": httpx.Response(204),
        "recently_played": None,
        "top": None,
        "library": None,
        "artist": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/token":
            return httpx.Response(200, json={"access_token": "novo", "expires_in": 3600})
        if path == "/v1/me/player/currently-playing":
            resposta = respostas["currently_playing"]
            return resposta(request) if callable(resposta) else resposta
        if path == "/v1/me/player/recently-played":
            resposta = respostas["recently_played"]
            if resposta is None:
                return httpx.Response(200, json={"items": [], "cursors": None})
            return resposta(request) if callable(resposta) else resposta
        if path.startswith("/v1/me/top/"):
            resposta = respostas["top"]
            if resposta is None:
                return httpx.Response(200, json={"items": []})
            return resposta(request) if callable(resposta) else resposta
        if path == "/v1/me/library/contains":
            resposta = respostas["library"]
            if resposta is None:
                return httpx.Response(200, json=[])
            return resposta(request) if callable(resposta) else resposta
        if path.startswith("/v1/artists/"):
            resposta = respostas["artist"]
            if resposta is None:
                return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1], "genres": []})
            return resposta(request) if callable(resposta) else resposta
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auth = SpotifyAuth("cid", "secret", "https://exemplo.com/spotify/callback", store, http)

    canal = FakeChannel(CHANNEL_ID)
    membros = {USER_A: FakeUser(USER_A, "Otávio"), USER_B: FakeUser(USER_B, "Namorada")}
    guild = FakeGuild(GUILD_ID, membros)
    discord_client = FakeDiscordClient(guild, {CHANNEL_ID: canal})
    discord_client.users.update(membros)

    listener = SpotifyListener(
        store=store,
        auth=auth,
        client=SpotifyClient(http),
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        allowed_user_ids=(USER_A, USER_B),
        oauth_host="127.0.0.1",
        oauth_port=0,
        http=http,
    )
    listener._discord = discord_client

    return {
        "store": store,
        "auth": auth,
        "listener": listener,
        "canal": canal,
        "membros": membros,
        "discord": discord_client,
        "respostas": respostas,
    }


ESCOPOS_COMPLETOS = (
    "user-read-currently-playing user-read-recently-played "
    "user-top-read user-library-read"
)


async def conectar(
    store: SpotifyStore,
    user_id: int,
    nome: str = "Conta",
    scope: str = ESCOPOS_COMPLETOS,
) -> None:
    await store.save_account(
        user_id, f"sp-{user_id}", nome, "access", "refresh",
        int(time.time()) + 3600, scope, int(time.time()),
    )


def proximo_tick(listener) -> None:
    """Simula a chegada do próximo tick: a agenda adaptativa venceu para todos.

    Sem isso, duas chamadas seguidas ao painel reaproveitam a leitura em cache —
    que é justamente o comportamento certo em produção.
    """
    listener._panel_fingerprint = None
    listener._next_poll.clear()


def mensagem(env, texto: str, autor_id: int = USER_A, canal=None, anexos=None) -> FakeMessage:
    canal = canal or env["canal"]
    autor = env["membros"].get(autor_id) or FakeUser(autor_id, "Intruso")
    return FakeMessage(
        texto, autor, canal,
        guild=FakeGuild(GUILD_ID, env["membros"]),
        attachments=anexos,
    )


# --------------------------------------------------------------------------- #
# Restricao de acesso
# --------------------------------------------------------------------------- #


class TestAcesso:
    async def test_terceiro_e_bloqueado(self, env):
        msg = mensagem(env, "!agora", autor_id=INTRUSO)
        await env["listener"].handle_message(env["discord"], msg)

        assert len(env["canal"].sent) == 1
        assert "privado" in env["canal"].sent[0].title.lower()

    async def test_canal_nao_configurado_e_ignorado(self, env):
        msg = mensagem(env, "!agora", canal=FakeChannel(OUTRO_CANAL))
        await env["listener"].handle_message(env["discord"], msg)

        assert msg.channel.sent == []

    async def test_outro_servidor_e_ignorado(self, env):
        msg = mensagem(env, "!agora")
        msg.guild = FakeGuild(123456789012345678, {})
        await env["listener"].handle_message(env["discord"], msg)

        assert env["canal"].sent == []

    async def test_terceiro_nao_consegue_ver_o_top_de_outra_pessoa(self, env):
        await conectar(env["store"], USER_A)
        msg = mensagem(env, f"!top <@{USER_A}>", autor_id=INTRUSO)
        await env["listener"].handle_message(env["discord"], msg)

        assert "privado" in env["canal"].sent[0].title.lower()

    async def test_comando_desconhecido_nao_e_tratado(self, env):
        msg = mensagem(env, "!tasks")
        await env["listener"].handle_message(env["discord"], msg)

        assert env["canal"].sent == []


# --------------------------------------------------------------------------- #
# Conexao e desconexao
# --------------------------------------------------------------------------- #


class TestConexao:
    async def test_conectar_manda_link_privado_no_dm(self, env):
        msg = mensagem(env, "!conectar")
        await env["listener"].handle_message(env["discord"], msg)

        dm = env["membros"][USER_A].dms[0]
        assert "accounts.spotify.com/authorize" in dm.description
        assert len(env["canal"].sent) == 1  # aviso no canal, sem o link

    async def test_cada_pessoa_conecta_a_propria_conta(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!conectar", USER_A))
        await env["listener"].handle_message(env["discord"], mensagem(env, "!conectar", USER_B))

        link_a = env["membros"][USER_A].dms[0].description
        link_b = env["membros"][USER_B].dms[0].description
        assert link_a.split("state=")[1] != link_b.split("state=")[1]

    async def test_desconectar_apaga_dados_da_pessoa(self, env):
        store = env["store"]
        await conectar(store, USER_A)
        await conectar(store, USER_B)
        from bot.spotify_store import Play

        await store.record_plays(
            [
                Play(USER_A, "t1", 1, "M", "A", [], None, None, None),
                Play(USER_B, "t1", 1, "M", "A", [], None, None, None),
            ]
        )

        await env["listener"].handle_message(env["discord"], mensagem(env, "!desconectar", USER_A))

        assert await store.get_account(USER_A) is None
        assert await store.get_account(USER_B) is not None
        assert await store.count_plays(USER_B, 0, 10) == 1

    async def test_desconectar_sem_conta_avisa(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!desconectar"))
        assert "não tem nenhuma conta" in env["canal"].sent[0].description


# --------------------------------------------------------------------------- #
# Painel e !agora
# --------------------------------------------------------------------------- #


class TestPainel:
    async def test_painel_e_criado_e_fixado(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())

        await env["listener"].refresh_panel()

        assert len(env["canal"].sent) == 1
        painel = list(env["canal"].messages.values())[0]
        assert painel.pinned is True
        assert await env["store"].get_value("panel_message_id") == str(painel.id)

    async def test_painel_nao_e_editado_quando_nada_muda(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())

        await env["listener"].refresh_panel()
        painel = list(env["canal"].messages.values())[0]
        await env["listener"].refresh_panel()
        await env["listener"].refresh_panel()

        assert painel.edits == 0
        assert len(env["canal"].sent) == 1

    async def test_painel_e_editado_quando_a_musica_muda(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())
        await env["listener"].refresh_panel()
        painel = list(env["canal"].messages.values())[0]

        env["respostas"]["currently_playing"] = httpx.Response(
            200, json=track_payload("track-2", "As It Was")
        )
        proximo_tick(env["listener"])
        await env["listener"].refresh_panel()

        assert painel.edits == 1
        assert len(env["canal"].sent) == 1  # editou, nao mandou outra mensagem

    async def test_painel_e_recuperado_apos_reinicio(self, env, tmp_path):
        """Novo processo lê o id salvo e edita a mensagem antiga em vez de criar outra."""
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())
        await env["listener"].refresh_panel()
        painel = list(env["canal"].messages.values())[0]

        # simula restart: o estado em memória se perde, canal e banco permanecem
        proximo_tick(env["listener"])
        env["listener"]._panel_message = None
        env["respostas"]["currently_playing"] = httpx.Response(
            200, json=track_payload("track-9", "Outra")
        )
        await env["listener"].refresh_panel()

        assert len(env["canal"].sent) == 1
        assert painel.edits == 1

    async def test_painel_apagado_com_o_bot_no_ar_e_recriado(self, env):
        """A edição falha com NotFound; o próximo tick recria e refixa."""
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())
        await env["listener"].refresh_panel()

        env["canal"].messages.clear()  # alguém apagou o painel
        env["respostas"]["currently_playing"] = httpx.Response(
            200, json=track_payload("track-2", "Outra")
        )
        proximo_tick(env["listener"])
        await env["listener"].refresh_panel()  # tenta editar, leva NotFound e limpa o estado
        await env["listener"].refresh_panel()  # recria

        assert len(env["canal"].sent) == 2
        assert await env["store"].get_value("panel_message_id") is not None

    async def test_painel_apagado_durante_o_restart_e_recriado(self, env):
        """Processo novo: o id salvo não existe mais no canal."""
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())
        await env["listener"].refresh_panel()

        env["canal"].messages.clear()
        env["listener"]._panel_message = None       # estado em memória some no restart
        proximo_tick(env["listener"])
        await env["listener"].refresh_panel()

        assert len(env["canal"].sent) == 2

    async def test_sem_reproducao_ativa_aparece_no_painel(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        env["respostas"]["currently_playing"] = httpx.Response(204)

        await env["listener"].refresh_panel()

        embed = env["canal"].sent[0]
        assert "Sem reprodução ativa" in embed.fields[0].value

    async def test_spotify_fora_do_ar_mantem_ultimo_dado_com_horario(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())
        await env["listener"].refresh_panel()

        env["respostas"]["currently_playing"] = httpx.Response(503)
        proximo_tick(env["listener"])
        await env["listener"].refresh_panel()

        painel = list(env["canal"].messages.values())[0]
        assert "Dados indisponíveis" in painel.embed.fields[0].value
        assert "último às" in painel.embed.fields[0].value

    async def test_autorizacao_expirada_pede_reconexao_no_painel(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(401, text="expired")

        await env["listener"].refresh_panel()

        assert "!conectar" in env["canal"].sent[0].fields[0].value
        assert (await env["store"].get_account(USER_A)).needs_reauth is True

    async def test_agora_responde_com_as_duas_contas(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")
        env["respostas"]["currently_playing"] = httpx.Response(200, json=track_payload())

        await env["listener"].handle_message(env["discord"], mensagem(env, "!agora"))

        embed = env["canal"].sent[0]
        assert [f.name for f in embed.fields] == ["Otávio", "Namorada"]

    async def test_agora_sem_ninguem_conectado(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!agora"))
        assert "!conectar" in env["canal"].sent[0].description


# --------------------------------------------------------------------------- #
# Coleta de escutas
# --------------------------------------------------------------------------- #


def recent_payload(items):
    return {
        "items": [
            {
                "played_at": played_at,
                "track": {
                    "id": track_id,
                    "name": nome,
                    "artists": [{"id": "a1", "name": "Artista"}],
                    "album": {"name": "Alb", "images": []},
                    "external_urls": {"spotify": "https://open.spotify.com/track/x"},
                },
            }
            for track_id, nome, played_at in items
        ],
        "cursors": None,
    }


class TestColeta:
    async def test_escutas_sao_registradas(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["recently_played"] = httpx.Response(
            200,
            json=recent_payload(
                [("t1", "Um", "2026-09-09T12:00:00.000Z"), ("t2", "Dois", "2026-09-09T12:04:00.000Z")]
            ),
        )

        await env["listener"].sync_recent_plays()

        assert await env["store"].count_plays(USER_A, 0, 9_999_999_999_999) == 2

    async def test_consultas_repetidas_nao_duplicam(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["recently_played"] = httpx.Response(
            200, json=recent_payload([("t1", "Um", "2026-09-09T12:00:00.000Z")])
        )

        await env["listener"].sync_recent_plays()
        await env["listener"].sync_recent_plays()
        await env["listener"].sync_recent_plays()

        assert await env["store"].count_plays(USER_A, 0, 9_999_999_999_999) == 1

    async def test_ouvir_a_mesma_faixa_de_novo_conta_outra_vez(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["recently_played"] = httpx.Response(
            200, json=recent_payload([("t1", "Um", "2026-09-09T12:00:00.000Z")])
        )
        await env["listener"].sync_recent_plays()

        env["respostas"]["recently_played"] = httpx.Response(
            200, json=recent_payload([("t1", "Um", "2026-09-09T15:30:00.000Z")])
        )
        await env["listener"].sync_recent_plays()

        assert await env["store"].count_plays(USER_A, 0, 9_999_999_999_999) == 2

    async def test_cursor_avanca_para_a_escuta_mais_recente(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["recently_played"] = httpx.Response(
            200,
            json=recent_payload(
                [("t1", "Um", "2026-09-09T12:00:00.000Z"), ("t2", "Dois", "2026-09-09T13:00:00.000Z")]
            ),
        )

        await env["listener"].sync_recent_plays()
        conta = await env["store"].get_account(USER_A)

        esperado = int(datetime(2026, 9, 9, 13, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)
        assert conta.last_played_at_ms == esperado

    async def test_conta_expirada_e_marcada_e_pulada(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["recently_played"] = httpx.Response(401, text="expired")

        await env["listener"].sync_recent_plays()

        assert (await env["store"].get_account(USER_A)).needs_reauth is True

    async def test_rate_limit_pausa_a_coleta(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["recently_played"] = httpx.Response(
            429, headers={"Retry-After": "30"}, text="slow"
        )

        await env["listener"].sync_recent_plays()

        assert env["listener"]._api.guard.blocked()

    async def test_item_sem_id_de_faixa_e_ignorado(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["recently_played"] = httpx.Response(
            200,
            json={
                "items": [{"played_at": "2026-09-09T12:00:00.000Z", "track": {"name": "Local"}}],
                "cursors": None,
            },
        )

        await env["listener"].sync_recent_plays()

        assert await env["store"].count_plays(USER_A, 0, 9_999_999_999_999) == 0


# --------------------------------------------------------------------------- #
# Janelas semanais no fuso de Brasilia
# --------------------------------------------------------------------------- #


class TestSemana:
    def test_semana_comeca_na_segunda_em_brasilia(self):
        quarta = datetime(2026, 9, 9, 15, 0, tzinfo=BRT)
        _, _, inicio, fim = week_bounds(quarta, 0)

        assert (inicio.year, inicio.month, inicio.day) == (2026, 9, 7)  # segunda
        assert inicio.hour == 0 and inicio.minute == 0
        assert (fim.month, fim.day) == (9, 14)
        assert str(inicio.tzinfo) == "America/Sao_Paulo"

    def test_semana_anterior(self):
        quarta = datetime(2026, 9, 9, 15, 0, tzinfo=BRT)
        _, _, inicio, fim = week_bounds(quarta, -1)

        assert (inicio.month, inicio.day) == (8, 31)
        assert (fim.month, fim.day) == (9, 7)

    def test_virada_de_semana_no_domingo_a_meia_noite_de_brasilia(self):
        """23h59 de domingo e 00h01 de segunda caem em semanas diferentes."""
        fim_domingo = datetime(2026, 9, 13, 23, 59, tzinfo=BRT)
        inicio_segunda = datetime(2026, 9, 14, 0, 1, tzinfo=BRT)

        _, _, dom_inicio, _ = week_bounds(fim_domingo, 0)
        _, _, seg_inicio, _ = week_bounds(inicio_segunda, 0)

        assert (dom_inicio.month, dom_inicio.day) == (9, 7)
        assert (seg_inicio.month, seg_inicio.day) == (9, 14)

    def test_meia_noite_utc_ainda_e_o_dia_anterior_em_brasilia(self):
        """00h30 UTC de segunda = 21h30 de domingo em Brasília, ou seja, semana anterior."""
        momento = datetime(2026, 9, 14, 0, 30, tzinfo=ZoneInfo("UTC")).astimezone(BRT)
        _, _, inicio, _ = week_bounds(momento, 0)

        assert (inicio.month, inicio.day) == (9, 7)

    def test_ultimos_sete_dias(self):
        domingo_20h = datetime(2026, 9, 13, 20, 0, tzinfo=BRT)
        _, _, inicio, fim = last_7_days(domingo_20h)

        assert fim - inicio == timedelta(days=7)
        assert (inicio.month, inicio.day, inicio.hour) == (9, 6, 20)

    def test_chave_da_semana_iso(self):
        assert iso_week_key(datetime(2026, 9, 9, tzinfo=BRT)) == "2026-W37"
        assert iso_week_key(datetime(2026, 9, 14, tzinfo=BRT)) == "2026-W38"

    def test_intervalo_legivel(self):
        inicio = datetime(2026, 9, 7, tzinfo=BRT)
        fim = datetime(2026, 9, 14, tzinfo=BRT)
        assert format_day_range(inicio, fim) == "07/09 a 13/09"

    def test_apelidos_de_periodo(self):
        assert parse_period(None) == "short_term"
        assert parse_period("4-semanas") == "short_term"
        assert parse_period("6-meses") == "medium_term"
        assert parse_period("1-ano") == "long_term"
        assert parse_period("decada") is None

        assert parse_week_offset(None) == 0
        assert parse_week_offset("semana") == 0
        assert parse_week_offset("passada") == -1
        assert parse_week_offset("mes-passado") is None


# --------------------------------------------------------------------------- #
# Resumo semanal
# --------------------------------------------------------------------------- #


class TestResumoSemanal:
    async def test_resumo_e_publicado_uma_unica_vez_na_semana(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        await env["listener"].publish_weekly_summary(force=True)
        antes = len(env["canal"].sent)

        # segunda chamada no mesmo domingo (ex.: restart do serviço) não republica
        await env["listener"].publish_weekly_summary()

        assert antes == 1
        assert len(env["canal"].sent) == 1

    async def test_resumo_sobrevive_a_reinicio_sem_duplicar(self, env, tmp_path):
        await conectar(env["store"], USER_A)
        await conectar(env["store"], USER_B)
        await env["listener"].publish_weekly_summary(force=True)

        # novo listener, mesmo banco: a marca de "já publicado" está persistida
        outro = SpotifyListener(
            store=env["store"],
            auth=env["auth"],
            client=SpotifyClient(httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(204)
            ))),
            guild_id=GUILD_ID,
            channel_id=CHANNEL_ID,
            allowed_user_ids=(USER_A, USER_B),
            oauth_host="127.0.0.1",
            oauth_port=0,
        )
        outro._discord = env["discord"]
        await outro.publish_weekly_summary()

        assert len(env["canal"].sent) == 1

    async def test_resumo_nao_marca_os_usuarios(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        await env["listener"].publish_weekly_summary(force=True)

        embed = env["canal"].sent[0]
        conteudo = embed.description + "".join(f.name + f.value for f in embed.fields)
        assert "<@" not in conteudo
        assert "Otávio" in conteudo

    async def test_semana_sem_atividade_publica_resumo_vazio(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        await env["listener"].publish_weekly_summary(force=True)

        embed = env["canal"].sent[0]
        assert "0** reproduções" in embed.fields[0].value
        assert "Nenhuma música em comum" in embed.fields[-1].value

    async def test_sem_contas_conectadas_nao_publica(self, env):
        await env["listener"].publish_weekly_summary(force=True)
        assert env["canal"].sent == []

    async def test_primeira_semana_incompleta_e_sinalizada(self, env):
        """Quem conectou no meio da janela recebe aviso de período parcial."""
        from bot.spotify_store import Play

        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        agora = datetime.now(BRT)
        ontem_ms = int((agora - timedelta(days=1)).timestamp() * 1000)
        await env["store"].record_plays(
            [Play(USER_A, "t1", ontem_ms, "Musica", "Artista", [], None, None, None)]
        )

        await env["listener"].publish_weekly_summary(force=True)

        assert "Período parcial" in env["canal"].sent[0].footer.text

    async def test_comparar_semana_atual_e_anterior(self, env):
        from bot.spotify_store import Play

        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        agora = datetime.now(BRT)
        inicio_ms, _, _, _ = week_bounds(agora, 0)
        await env["store"].record_plays(
            [
                Play(USER_A, "t1", inicio_ms + 1000, "Comum", "Artista", [], None, None, None),
                Play(USER_B, "t1", inicio_ms + 2000, "Comum", "Artista", [], None, None, None),
            ]
        )

        await env["listener"].handle_message(env["discord"], mensagem(env, "!comparar semana"))
        atual = env["canal"].sent[0]
        assert "Comum" in atual.fields[-1].value  # ouviram os dois

        await env["listener"].handle_message(env["discord"], mensagem(env, "!comparar passada"))
        passada = env["canal"].sent[1]
        assert "Nenhuma música em comum" in passada.fields[-1].value

    async def test_comparar_periodo_invalido(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!comparar ontem"))
        assert "inválido" in env["canal"].sent[0].title


# --------------------------------------------------------------------------- #
# !top
# --------------------------------------------------------------------------- #


class TestTop:
    async def test_top_usa_o_ranking_do_spotify_e_deixa_isso_claro(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        env["respostas"]["top"] = httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "Cinema",
                        "artists": [{"name": "Harry Styles"}],
                        "external_urls": {"spotify": "https://open.spotify.com/track/1"},
                    }
                ]
            },
        )

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        embed = env["canal"].sent[0]
        assert "Ranking do Spotify" in embed.description
        assert "não pelas escutas registradas" in embed.footer.text
        assert "Cinema" in embed.fields[0].value

    async def test_top_de_outra_pessoa_autorizada(self, env):
        await conectar(env["store"], USER_B, "Namorada")
        await env["listener"].handle_message(
            env["discord"], mensagem(env, f"!top <@{USER_B}> 6-meses")
        )

        embed = env["canal"].sent[0]
        assert "Namorada" in embed.title
        assert "6 meses" in embed.description

    async def test_top_de_quem_nao_conectou(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))
        assert "ainda não conectou" in env["canal"].sent[0].description

    async def test_periodo_invalido_avisa(self, env):
        await conectar(env["store"], USER_A)
        await env["listener"].handle_message(env["discord"], mensagem(env, "!top decada"))
        assert "inválido" in env["canal"].sent[0].title

    async def test_spotify_indisponivel_no_top(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["top"] = httpx.Response(503)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        assert "indisponível" in env["canal"].sent[0].title


# --------------------------------------------------------------------------- #
# Limites do Discord
# --------------------------------------------------------------------------- #


class TestLimitesDeEmbed:
    """Campo de embed aceita no máximo 1024 caracteres; estourar derruba o comando."""

    def test_fit_field_trunca_e_avisa(self):
        from bot.spotify_format import FIELD_LIMIT, fit_field

        linhas = [f"`{i:>2}.` " + "x" * 120 for i in range(1, 11)]
        valor = fit_field(linhas)

        assert len(valor) <= FIELD_LIMIT
        assert "não couberam" in valor

    def test_fit_field_mantem_tudo_quando_cabe(self):
        from bot.spotify_format import fit_field

        assert fit_field(["uma", "duas"]) == "uma\nduas"
        assert fit_field([]) == ""

    async def test_top_com_nomes_longos_nao_estoura(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        env["respostas"]["top"] = httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "Nobody Gets Me (feat. Alguem) - Extended Club Mix Deluxe",
                        "artists": [
                            {"name": "SZA"},
                            {"name": "Travis Scott"},
                            {"name": "Kendrick Lamar"},
                        ],
                        "external_urls": {
                            "spotify": "https://open.spotify.com/track/1Qrg8KqiBpW07V7PNxwwwL"
                        },
                    }
                    for _ in range(10)
                ]
            },
        )

        await env["listener"].handle_message(env["discord"], mensagem(env, "!top"))

        embed = env["canal"].sent[0]
        for campo in embed.fields:
            assert len(campo.value) <= 1024, f"campo '{campo.name}' estourou"

    async def test_comparar_com_muitas_faixas_nao_estoura(self, env):
        from bot.spotify_store import Play

        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        agora = datetime.now(BRT)
        inicio_ms, _, _, _ = week_bounds(agora, 0)
        nome_longo = "Título Absurdamente Longo De Música Para Testar O Limite Do Embed"

        escutas = []
        for i in range(12):
            for user in (USER_A, USER_B):
                escutas.append(
                    Play(
                        user, f"t{i}", inicio_ms + i * 1000 + user % 10,
                        f"{nome_longo} {i}", "Artista Com Nome Bem Comprido Também",
                        [f"a{i}"], None, None,
                        "https://open.spotify.com/track/1Qrg8KqiBpW07V7PNxwwwL",
                    )
                )
        await env["store"].record_plays(escutas)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!comparar semana"))

        embed = env["canal"].sent[0]
        for campo in embed.fields:
            assert len(campo.value) <= 1024, f"campo '{campo.name}' estourou"


class TestAgregacaoDeArtistas:
    async def test_artistas_homonimos_nao_sao_somados(self, env):
        """Dois artistas com o mesmo nome e ids diferentes contam separado."""
        from bot.spotify_store import Play

        await conectar(env["store"], USER_A)
        escutas = [
            Play(USER_A, "t1", 1, "M1", "Nomes Iguais", ["id-um"], None, None, None),
            Play(USER_A, "t2", 2, "M2", "Nomes Iguais", ["id-um"], None, None, None),
            Play(USER_A, "t3", 3, "M3", "Nomes Iguais", ["id-dois"], None, None, None),
        ]
        await env["store"].record_plays(escutas)

        topo = await env["store"].top_artists(USER_A, 0, 100, limit=5)

        assert [a["plays"] for a in topo] == [2, 1]

    async def test_artista_com_virgula_no_nome(self, env):
        from bot.spotify_store import Play

        await conectar(env["store"], USER_A)
        await env["store"].record_plays(
            [Play(USER_A, "t1", 1, "M1", "Earth, Wind & Fire", ["id-ewf"], None, None, None)]
        )

        topo = await env["store"].top_artists(USER_A, 0, 100, limit=5)

        assert topo[0]["plays"] == 1
