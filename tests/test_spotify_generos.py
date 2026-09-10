"""!generos — perfil de gênero, com a ressalva de que o campo do Spotify está furado."""

import httpx

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
