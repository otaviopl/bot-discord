"""!generos — perfil de gênero, com a ressalva de que o campo do Spotify está furado."""

import httpx
import pytest

from bot.spotify_store import Play

# `env` é a fixture compartilhada com os testes do listener — importada de propósito.
from tests.test_spotify_listener import (  # noqa: F401
    USER_A, USER_B, conectar, env, mensagem,
)

BASE = 1_700_000_000_000
MIN = 60_000


def play(user, artist_id, artista, offset=0, track=None):
    return Play(
        user, track or f"t{offset}", BASE + offset * MIN, f"Faixa {offset}", artista,
        [artist_id], "Album", None, None, duration_ms=3 * MIN,
    )


def artistas(mapa):
    """Responde /artists/{id} com os gêneros do mapa."""
    def handler(request):
        artist_id = str(request.url.path).rsplit("/", 1)[-1]
        return httpx.Response(
            200, json={"id": artist_id, "name": artist_id, "genres": mapa.get(artist_id, [])}
        )
    return handler


class TestPerfil:
    async def test_generos_ponderados_pelas_escutas(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["store"].record_plays([
            play(USER_A, "a1", "Rock Band", 0),
            play(USER_A, "a1", "Rock Band", 1),
            play(USER_A, "a1", "Rock Band", 2),
            play(USER_A, "a2", "Samba Man", 3),
        ])
        env["respostas"]["artist"] = artistas({"a1": ["indie rock"], "a2": ["samba"]})

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        campo = env["canal"].sent[0].fields[0]
        assert "indie rock" in campo.value
        assert "75%" in campo.value  # 3 de 4 escutas
        assert "samba" in campo.value

    async def test_generos_em_comum(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Gi")
        await env["store"].record_plays([
            play(USER_A, "a1", "Band A", 0),
            play(USER_B, "a2", "Band B", 1, track="t99"),
        ])
        env["respostas"]["artist"] = artistas({
            "a1": ["indie rock", "soul"],
            "a2": ["mpb", "soul"],
        })

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        campo = next(f for f in env["canal"].sent[0].fields if "encontram" in f.name)
        assert "soul" in campo.value
        assert "indie rock" not in campo.value

    async def test_sem_genero_em_comum(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Gi")
        await env["store"].record_plays([
            play(USER_A, "a1", "Band A", 0),
            play(USER_B, "a2", "Band B", 1, track="t99"),
        ])
        env["respostas"]["artist"] = artistas({"a1": ["indie rock"], "a2": ["mpb"]})

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        campo = next(f for f in env["canal"].sent[0].fields if "encontram" in f.name)
        assert "Nenhum gênero em comum" in campo.value


class TestCampoFurado:
    """O Spotify marcou `genres` como descontinuado e devolve vazio para muitos
    artistas. O comando precisa dizer isso, não fingir que não achou nada."""

    async def test_todos_sem_classificacao(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["store"].record_plays([play(USER_A, "a1", "Band A", 0)])
        env["respostas"]["artist"] = artistas({})  # tudo vazio

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        embed = env["canal"].sent[0]
        assert "não classificou" in embed.fields[0].value
        assert "0% dos artistas" in embed.footer.text
        assert "descontinuado" in embed.footer.text

    async def test_cobertura_parcial_aparece_no_rodape(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["store"].record_plays([
            play(USER_A, "a1", "Band A", 0),
            play(USER_A, "a2", "Band B", 1),
            play(USER_A, "a3", "Band C", 2),
            play(USER_A, "a4", "Band D", 3),
        ])
        env["respostas"]["artist"] = artistas({"a1": ["rock"], "a2": ["mpb"]})

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        assert "50% dos artistas" in env["canal"].sent[0].footer.text

    async def test_sem_escutas_no_periodo(self, env):
        await conectar(env["store"], USER_A, "Otávio")

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        assert "Nada registrado" in env["canal"].sent[0].fields[0].value


class TestCache:
    async def test_artista_ja_consultado_nao_volta_para_a_api(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["store"].record_plays([play(USER_A, "a1", "Band A", 0)])

        chamadas = []

        def handler(request):
            chamadas.append(str(request.url.path))
            return httpx.Response(200, json={"id": "a1", "name": "Band A", "genres": ["rock"]})

        env["respostas"]["artist"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))
        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        assert len(chamadas) == 1, "a segunda consulta deve sair do cache"

    async def test_resposta_vazia_tambem_e_cacheada(self, env):
        """Senão cada consulta refaria a chamada para todo artista sem gênero."""
        await conectar(env["store"], USER_A, "Otávio")
        await env["store"].record_plays([play(USER_A, "a1", "Band A", 0)])

        chamadas = []

        def handler(request):
            chamadas.append(1)
            return httpx.Response(200, json={"id": "a1", "name": "Band A", "genres": []})

        env["respostas"]["artist"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))
        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        assert len(chamadas) == 1

    async def test_falha_na_api_nao_derruba_o_comando(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["store"].record_plays([play(USER_A, "a1", "Band A", 0)])
        env["respostas"]["artist"] = httpx.Response(503)

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        # responde mesmo assim, sem gênero nenhum
        assert "Perfil de gênero" in env["canal"].sent[0].title


class TestAcesso:
    async def test_terceiro_bloqueado(self, env):
        await env["listener"].handle_message(
            env["discord"], mensagem(env, "!generos", autor_id=333333333333333333)
        )
        assert "privado" in env["canal"].sent[0].title.lower()

    async def test_aceita_com_acento(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await env["listener"].handle_message(env["discord"], mensagem(env, "!gêneros tudo"))
        assert "Perfil de gênero" in env["canal"].sent[0].title


class TestLimiteDeConsultas:
    """Sem teto, o primeiro !generos disparava até 40 chamadas por pessoa de uma vez."""

    @pytest.fixture(autouse=True)
    def sem_pausa(self, monkeypatch):
        import bot.spotify_listener as modulo
        monkeypatch.setattr(modulo, "GENEROS_PAUSA_S", 0)

    async def _com_artistas(self, env, quantidade):
        await conectar(env["store"], USER_A, "Otávio")
        # artista 0 é o mais ouvido, o último é o menos
        escutas = []
        offset = 0
        for i in range(quantidade):
            for _ in range(quantidade - i):
                escutas.append(play(USER_A, f"a{i}", f"Band {i}", offset))
                offset += 1
        await env["store"].record_plays(escutas)

    async def test_primeira_execucao_consulta_no_maximo_dez(self, env):
        from bot.spotify_listener import GENEROS_NOVOS_POR_VEZ

        await self._com_artistas(env, 25)
        chamadas = []

        def handler(request):
            artist_id = str(request.url.path).rsplit("/", 1)[-1]
            chamadas.append(artist_id)
            return httpx.Response(200, json={"id": artist_id, "name": artist_id, "genres": ["rock"]})

        env["respostas"]["artist"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        assert len(chamadas) == GENEROS_NOVOS_POR_VEZ
        assert "Faltam 15 artista(s)" in env["canal"].sent[0].footer.text

    async def test_os_mais_ouvidos_vem_primeiro(self, env):
        await self._com_artistas(env, 25)
        chamadas = []

        def handler(request):
            artist_id = str(request.url.path).rsplit("/", 1)[-1]
            chamadas.append(artist_id)
            return httpx.Response(200, json={"id": artist_id, "genres": []})

        env["respostas"]["artist"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        assert chamadas == [f"a{i}" for i in range(10)]

    async def test_execucoes_seguintes_completam_o_cache(self, env):
        await self._com_artistas(env, 25)
        chamadas = []

        def handler(request):
            artist_id = str(request.url.path).rsplit("/", 1)[-1]
            chamadas.append(artist_id)
            return httpx.Response(200, json={"id": artist_id, "genres": ["rock"]})

        env["respostas"]["artist"] = handler

        for _ in range(4):
            await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        assert len(chamadas) == 25, "10 + 10 + 5 e depois nada"
        assert len(set(chamadas)) == 25, "nenhum artista consultado duas vezes"
        assert "Faltam" not in (env["canal"].sent[-1].footer.text or "")

    async def test_bloqueio_do_spotify_nao_gasta_chamada(self, env):
        await self._com_artistas(env, 5)
        await env["listener"]._api.guard.block(33715, "QUOTA_EXCEEDED")
        chamadas = []

        def handler(request):
            chamadas.append(1)
            return httpx.Response(200, json={"genres": []})

        env["respostas"]["artist"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!generos tudo"))

        embed = env["canal"].sent[0]
        assert chamadas == []
        assert "em pausa" in embed.footer.text
        assert "Ainda não consegui consultar" in embed.fields[0].value
        assert "Nada registrado" not in embed.fields[0].value
