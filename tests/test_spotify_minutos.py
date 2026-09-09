"""Camada 2 (amostragem do player), camada 3 (import) e o comando !minutos."""

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta

import httpx
import pytest
from cryptography.fernet import Fernet

from bot.spotify_import import ImportError_, parse_upload, resumo
from bot.spotify_listening import format_duration
from bot.spotify_store import Play, SpotifyStore

from tests.conftest import FakeAttachment
# `env` é a fixture compartilhada com os testes do listener — importada de propósito.
from tests.test_spotify_listener import (  # noqa: F401
    BRT, USER_A, USER_B, conectar, env, mensagem, track_payload,
)

MIN = 60_000


# --------------------------------------------------------------------------- #
# Camada 2: amostragem do progress_ms
# --------------------------------------------------------------------------- #


def playing(track_id="t1", progress_ms=0):
    payload = track_payload(track_id)
    payload["progress_ms"] = progress_ms
    return httpx.Response(200, json=payload)


class TestAmostragem:
    async def test_primeira_amostra_conta_o_progresso_ja_tocado(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = playing("t1", 45_000)

        await env["listener"].refresh_panel()

        sessoes = await env["store"].raw_measured(USER_A, 0, 9_999_999_999_999)
        assert len(sessoes) == 1
        assert sessoes[0]["ms_played"] == 45_000

    async def test_avanco_do_progresso_acumula(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = playing("t1", 10_000)
        await env["listener"].refresh_panel()

        env["respostas"]["currently_playing"] = playing("t1", 70_000)
        env["listener"]._panel_fingerprint = None
        await env["listener"].refresh_panel()

        sessoes = await env["store"].raw_measured(USER_A, 0, 9_999_999_999_999)
        # 10s da primeira amostra + no máximo o tempo de relógio decorrido entre elas
        assert sessoes[0]["ms_played"] >= 10_000

    async def test_pausado_nao_acumula_tempo(self, env):
        """Progresso parado entre amostras significa que nada tocou."""
        await conectar(env["store"], USER_A)
        pausado = track_payload("t1", is_playing=False)
        pausado["progress_ms"] = 30_000
        env["respostas"]["currently_playing"] = httpx.Response(200, json=pausado)

        await env["listener"].refresh_panel()
        env["listener"]._panel_fingerprint = None
        await env["listener"].refresh_panel()

        sessoes = await env["store"].raw_measured(USER_A, 0, 9_999_999_999_999)
        assert sessoes[0]["ms_played"] == 30_000  # não cresceu na segunda amostra

    async def test_troca_de_faixa_abre_sessao_nova(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = playing("t1", 60_000)
        await env["listener"].refresh_panel()

        env["respostas"]["currently_playing"] = playing("t2", 20_000)
        env["listener"]._panel_fingerprint = None
        await env["listener"].refresh_panel()

        sessoes = await env["store"].raw_measured(USER_A, 0, 9_999_999_999_999)
        assert {s["track_id"] for s in sessoes} == {"t1", "t2"}

    async def test_pulo_para_frente_nao_infla_o_tempo(self, env):
        """Arrastar a barra para o fim não pode virar tempo ouvido."""
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = playing("t1", 5_000)
        await env["listener"].refresh_panel()

        env["respostas"]["currently_playing"] = playing("t1", 200_000)  # scrub
        env["listener"]._panel_fingerprint = None
        await env["listener"].refresh_panel()

        sessoes = await env["store"].raw_measured(USER_A, 0, 9_999_999_999_999)
        # o avanço é limitado ao tempo real decorrido, que nos testes é ~0
        assert sessoes[0]["ms_played"] < 20_000

    async def test_sem_reproducao_nao_grava_nada(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(204)

        await env["listener"].refresh_panel()

        assert await env["store"].raw_measured(USER_A, 0, 9_999_999_999_999) == []

    async def test_anuncio_nao_vira_tempo_ouvido(self, env):
        await conectar(env["store"], USER_A)
        env["respostas"]["currently_playing"] = httpx.Response(
            200, json={"is_playing": True, "currently_playing_type": "ad",
                       "item": None, "progress_ms": 15_000}
        )

        await env["listener"].refresh_panel()

        assert await env["store"].raw_measured(USER_A, 0, 9_999_999_999_999) == []


# --------------------------------------------------------------------------- #
# Camada 3: import do arquivo do Spotify
# --------------------------------------------------------------------------- #


def extended_entry(ts, ms_played, nome="Cinema", artista="Harry Styles", track="abc123"):
    return {
        "ts": ts,
        "ms_played": ms_played,
        "master_metadata_track_name": nome,
        "master_metadata_album_artist_name": artista,
        "master_metadata_album_album_name": "Album",
        "spotify_track_uri": f"spotify:track:{track}" if track else None,
    }


def make_zip(arquivos: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for nome, conteudo in arquivos.items():
            z.writestr(nome, json.dumps(conteudo))
    return buffer.getvalue()


class TestImport:
    def test_formato_extended(self):
        dados = json.dumps([extended_entry("2026-01-15T20:30:00Z", 210_000)]).encode()
        resultado = parse_upload(dados, "Streaming_History_Audio_2026.json")

        entrada = resultado["entradas"][0]
        assert entrada["ms_played"] == 210_000
        assert entrada["track_id"] == "abc123"
        assert entrada["track_name"] == "Cinema"
        # ts marca o FIM: o início é calculado para trás
        assert entrada["ended_ms"] - entrada["started_ms"] == 210_000

    def test_formato_antigo(self):
        dados = json.dumps([
            {"endTime": "2026-01-15 20:30", "msPlayed": 180_000,
             "trackName": "Cinema", "artistName": "Harry Styles"}
        ]).encode()
        resultado = parse_upload(dados, "StreamingHistory0.json")

        entrada = resultado["entradas"][0]
        assert entrada["ms_played"] == 180_000
        assert entrada["track_id"] is None
        assert entrada["track_key"].startswith("h:")  # chave derivada de nome+artista

    def test_podcast_e_ignorado(self):
        dados = json.dumps([
            {"ts": "2026-01-15T20:30:00Z", "ms_played": 600_000,
             "episode_name": "Ep 12", "spotify_episode_uri": "spotify:episode:x"},
            extended_entry("2026-01-15T21:00:00Z", 200_000),
        ]).encode()

        resultado = parse_upload(dados, "hist.json")
        assert len(resultado["entradas"]) == 1

    def test_reproducao_curtissima_e_descartada(self):
        dados = json.dumps([extended_entry("2026-01-15T20:30:00Z", 300)]).encode()
        assert parse_upload(dados, "hist.json")["entradas"] == []

    def test_zip_com_varios_arquivos(self):
        dados = make_zip({
            "Spotify Extended Streaming History/Streaming_History_Audio_2024_1.json":
                [extended_entry("2024-05-01T10:00:00Z", 200_000)],
            "Spotify Extended Streaming History/Streaming_History_Audio_2025_2.json":
                [extended_entry("2025-05-01T10:00:00Z", 300_000)],
        })

        resultado = parse_upload(dados, "meus_dados.zip")

        assert resultado["arquivos"] == 2
        assert len(resultado["entradas"]) == 2

    def test_zip_ignora_arquivos_que_nao_sao_historico(self):
        dados = make_zip({
            "Streaming_History_Audio_2024_1.json": [extended_entry("2024-05-01T10:00:00Z", 200_000)],
            "Playlist1.json": {"playlists": []},
            "Userdata.json": {"email": "x@y.z"},
        })

        resultado = parse_upload(dados, "dados.zip")

        assert resultado["arquivos"] == 1
        assert len(resultado["ignorados"]) == 2

    def test_zip_sem_historico_de_audio_explica_o_erro(self):
        dados = make_zip({"Playlist1.json": {"playlists": []}})

        with pytest.raises(ImportError_, match="histórico de áudio"):
            parse_upload(dados, "dados.zip")

    def test_arquivo_de_outro_tipo_e_recusado(self):
        with pytest.raises(ImportError_, match="zip"):
            parse_upload(b"qualquer coisa", "foto.png")

    def test_json_invalido(self):
        with pytest.raises(ImportError_, match="JSON"):
            parse_upload(b"{nao eh json", "hist.json")

    def test_json_que_nao_e_lista(self):
        with pytest.raises(ImportError_, match="lista"):
            parse_upload(b'{"a": 1}', "hist.json")

    def test_zip_corrompido(self):
        with pytest.raises(ImportError_, match="corrompido"):
            parse_upload(b"PK\x03\x04lixo", "dados.zip")

    def test_zip_com_arquivos_demais_e_recusado(self):
        """Proteção contra zip bomb."""
        dados = make_zip({f"Streaming_History_Audio_{i}.json": [] for i in range(250)})

        with pytest.raises(ImportError_, match="limite"):
            parse_upload(dados, "dados.zip")

    def test_resumo_do_import(self):
        entradas = parse_upload(
            json.dumps([
                extended_entry("2026-01-15T20:30:00Z", 200_000),
                extended_entry("2026-02-15T20:30:00Z", 300_000),
            ]).encode(),
            "hist.json",
        )["entradas"]

        stats = resumo(entradas)
        assert stats["total"] == 2
        assert stats["ms"] == 500_000


class TestImportPersistencia:
    @pytest.fixture
    def store(self, tmp_path):
        return SpotifyStore(str(tmp_path / "s.db"), Fernet.generate_key().decode())

    async def test_import_repetido_nao_duplica(self, store):
        entradas = parse_upload(
            json.dumps([extended_entry("2026-01-15T20:30:00Z", 200_000)]).encode(), "h.json"
        )["entradas"]

        primeira = await store.record_imported(USER_A, entradas)
        segunda = await store.record_imported(USER_A, entradas)

        assert primeira == 1
        assert segunda == 0

    async def test_desconectar_apaga_o_importado(self, store):
        await store.save_account(USER_A, "sp", "N", "a", "r", 9_999_999_999, "s", 1)
        entradas = parse_upload(
            json.dumps([extended_entry("2026-01-15T20:30:00Z", 200_000)]).encode(), "h.json"
        )["entradas"]
        await store.record_imported(USER_A, entradas)

        await store.delete_account(USER_A)

        assert await store.raw_imported(USER_A, 0, 9_999_999_999_999) == []


# --------------------------------------------------------------------------- #
# Migração do banco
# --------------------------------------------------------------------------- #


class TestMigracao:
    async def test_banco_antigo_ganha_a_coluna_sem_perder_dados(self, tmp_path):
        """Banco criado antes desta versão continua funcionando."""
        caminho = str(tmp_path / "antigo.db")
        chave = Fernet.generate_key().decode()

        # simula o schema anterior, sem duration_ms
        with sqlite3.connect(caminho) as conn:
            conn.executescript("""
                CREATE TABLE spotify_plays (
                    discord_user_id TEXT NOT NULL,
                    track_id        TEXT NOT NULL,
                    played_at_ms    INTEGER NOT NULL,
                    track_name      TEXT NOT NULL,
                    artists         TEXT NOT NULL,
                    artist_ids      TEXT,
                    album_name      TEXT,
                    album_image     TEXT,
                    track_url       TEXT,
                    PRIMARY KEY (discord_user_id, track_id, played_at_ms)
                );
            """)
            conn.execute(
                "INSERT INTO spotify_plays (discord_user_id, track_id, played_at_ms, "
                "track_name, artists) VALUES (?, ?, ?, ?, ?)",
                (str(USER_A), "t1", 1_700_000_000_000, "Antiga", "Artista"),
            )

        store = SpotifyStore(caminho, chave)  # aplica a migração

        assert await store.count_plays(USER_A, 0, 9_999_999_999_999) == 1
        linhas = await store.raw_plays(USER_A, 0, 9_999_999_999_999)
        assert linhas[0]["duration_ms"] is None  # escuta antiga, sem duração

        # e escutas novas já entram com duração
        await store.record_plays([
            Play(USER_A, "t2", 1_700_000_100_000, "Nova", "Artista", [], None, None, None,
                 duration_ms=180_000)
        ])
        linhas = await store.raw_plays(USER_A, 0, 9_999_999_999_999)
        assert linhas[1]["duration_ms"] == 180_000

    async def test_migracao_e_idempotente(self, tmp_path):
        caminho = str(tmp_path / "s.db")
        chave = Fernet.generate_key().decode()

        SpotifyStore(caminho, chave)
        store = SpotifyStore(caminho, chave)  # segunda abertura não pode falhar

        assert await store.raw_plays(USER_A, 0, 10) == []


# --------------------------------------------------------------------------- #
# Comando !minutos
# --------------------------------------------------------------------------- #


class TestComandoMinutos:
    async def test_minutos_do_mes_com_estimativa(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        agora = datetime.now(BRT)
        inicio_mes = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        base = int((inicio_mes + timedelta(hours=1)).timestamp() * 1000)

        await env["store"].record_plays([
            Play(USER_A, "t1", base, "M1", "A", [], None, None, None, duration_ms=3 * MIN),
            Play(USER_A, "t2", base + 10 * MIN, "M2", "A", [], None, None, None, duration_ms=3 * MIN),
        ])

        await env["listener"].handle_message(env["discord"], mensagem(env, "!minutos"))

        embed = env["canal"].sent[0]
        assert "Tempo ouvido" in embed.title
        assert "estimados" in embed.fields[0].value
        assert "não expõe minutos ouvidos" in embed.footer.text

    async def test_importado_vira_numero_real_e_muda_o_rodape(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        agora = datetime.now(BRT)
        fim = int(agora.timestamp() * 1000) - MIN
        await env["store"].record_imported(USER_A, [{
            "started_ms": fim - 30 * MIN, "ended_ms": fim, "ms_played": 30 * MIN,
            "track_key": "t1", "track_id": "t1", "track_name": "M", "artists": "A",
        }])

        await env["listener"].handle_message(env["discord"], mensagem(env, "!minutos hoje"))

        embed = env["canal"].sent[0]
        assert "30min do histórico do Spotify" in embed.fields[0].value
        assert "Número real" in embed.footer.text

    async def test_periodo_invalido(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!minutos decada"))
        assert "inválido" in env["canal"].sent[0].title

    async def test_sem_contas_conectadas(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!minutos"))
        assert "!conectar" in env["canal"].sent[0].description

    async def test_terceiro_bloqueado(self, env):
        await env["listener"].handle_message(
            env["discord"], mensagem(env, "!minutos", autor_id=333333333333333333)
        )
        assert "privado" in env["canal"].sent[0].title.lower()

    async def test_importar_sem_anexo_explica_como_conseguir(self, env):
        await conectar(env["store"], USER_A)
        await env["listener"].handle_message(env["discord"], mensagem(env, "!importar"))

        embed = env["canal"].sent[0]
        assert "Privacidade" in embed.description
        assert "Extended streaming history" in embed.description

    async def test_importar_sem_conta_conectada(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!importar"))
        assert "!conectar" in env["canal"].sent[0].description


class TestFormatacaoDuracao:
    def test_casos(self):
        assert format_duration(0) == "0min"
        assert format_duration(90_000) == "2min"
        assert format_duration(3_600_000) == "1h"
        assert format_duration(5_400_000) == "1h 30min"


class TestImportPeloComando:
    """Fluxo completo: anexo no Discord → banco → !minutos usa o número real."""

    async def test_import_com_anexo_grava_e_confirma(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        conteudo = json.dumps([
            extended_entry("2026-01-15T20:30:00Z", 200_000),
            extended_entry("2026-01-15T20:40:00Z", 180_000, nome="Outra", track="def456"),
        ]).encode()
        anexo = FakeAttachment("Streaming_History_Audio_2026.json", conteudo)

        await env["listener"].handle_message(
            env["discord"], mensagem(env, "!importar", anexos=[anexo])
        )

        embed = env["canal"].sent[0]
        assert "Histórico importado" in embed.title
        assert "**2** reproduções novas" in embed.description

        linhas = await env["store"].raw_imported(USER_A, 0, 9_999_999_999_999)
        assert len(linhas) == 2

    async def test_reimportar_avisa_das_duplicatas(self, env):
        await conectar(env["store"], USER_A)
        conteudo = json.dumps([extended_entry("2026-01-15T20:30:00Z", 200_000)]).encode()
        anexo = FakeAttachment("hist.json", conteudo)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!importar", anexos=[anexo]))
        await env["listener"].handle_message(env["discord"], mensagem(env, "!importar", anexos=[anexo]))

        segundo = env["canal"].sent[1]
        assert "**0** reproduções novas" in segundo.description
        assert any("Duplicatas" in f.name for f in segundo.fields)

    async def test_arquivo_invalido_nao_derruba_o_comando(self, env):
        await conectar(env["store"], USER_A)
        anexo = FakeAttachment("foto.png", b"\x89PNG\r\n")

        await env["listener"].handle_message(env["discord"], mensagem(env, "!importar", anexos=[anexo]))

        assert "não serve" in env["canal"].sent[0].title

    async def test_anexo_grande_demais_e_recusado(self, env):
        await conectar(env["store"], USER_A)
        anexo = FakeAttachment("gigante.zip", b"x" * (26 * 1024 * 1024))

        await env["listener"].handle_message(env["discord"], mensagem(env, "!importar", anexos=[anexo]))

        assert "grande demais" in env["canal"].sent[0].title
        assert await env["store"].raw_imported(USER_A, 0, 9_999_999_999_999) == []

    async def test_importado_tem_prioridade_sobre_a_estimativa_no_comando(self, env):
        """O mesmo período coberto pelas duas fontes conta uma vez, pelo número real."""
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Namorada")

        agora = datetime.now(BRT)
        fim_ms = int(agora.timestamp() * 1000) - MIN
        inicio_ms = fim_ms - 4 * MIN

        # estimativa cobrindo o mesmo intervalo
        await env["store"].record_plays([
            Play(USER_A, "t1", inicio_ms, "M", "A", [], None, None, None, duration_ms=4 * MIN),
        ])
        # e o dado real
        await env["store"].record_imported(USER_A, [{
            "started_ms": inicio_ms, "ended_ms": fim_ms, "ms_played": 4 * MIN,
            "track_key": "t1", "track_id": "t1", "track_name": "M", "artists": "A",
        }])

        await env["listener"].handle_message(env["discord"], mensagem(env, "!minutos hoje"))

        valor = env["canal"].sent[0].fields[0].value
        assert "**4min**" in valor           # não 8min
        assert "estimados" not in valor
