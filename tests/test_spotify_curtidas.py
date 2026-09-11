"""!curtidas — interseção das bibliotecas, não só das escutas."""

import httpx

from bot.spotify_store import Play

# `env` é a fixture compartilhada com os testes do listener — importada de propósito.
from tests.test_spotify_listener import (  # noqa: F401
    USER_A, USER_B, conectar, env, mensagem,
)

BASE = 1_700_000_000_000
MIN = 60_000


def play(user, track_id, nome, offset=0, url=True):
    return Play(
        user, track_id, BASE + offset * MIN, nome, "Artista", ["a1"], "Album", None,
        f"https://open.spotify.com/track/{track_id}" if url else None, duration_ms=3 * MIN,
    )


def biblioteca(salvos_por_token):
    """Responde library/contains marcando como salvas as faixas indicadas."""
    chamadas = {"n": 0}

    def handler(request):
        uris = request.url.params.get("uris", "").split(",")
        ids = [u.rsplit(":", 1)[-1] for u in uris if u]
        # alterna entre as duas pessoas conforme a ordem das chamadas
        indice = chamadas["n"]
        chamadas["n"] += 1
        salvos = salvos_por_token[min(indice, len(salvos_por_token) - 1)]
        return httpx.Response(200, json=[tid in salvos for tid in ids])

    return handler


async def com_escutas_em_comum(env):
    await conectar(env["store"], USER_A, "Otávio")
    await conectar(env["store"], USER_B, "Gi")
    await env["store"].record_plays([
        play(USER_A, "t1", "Comum 1"), play(USER_B, "t1", "Comum 1", 1),
        play(USER_A, "t2", "Comum 2", 2), play(USER_B, "t2", "Comum 2", 3),
        play(USER_A, "t3", "Comum 3", 4), play(USER_B, "t3", "Comum 3", 5),
    ])


class TestCurtidas:
    async def test_lista_o_que_os_dois_salvaram(self, env):
        await com_escutas_em_comum(env)
        # Otávio salvou t1 e t2; Gi salvou t1 e t3 → só t1 é de ambos
        env["respostas"]["library"] = biblioteca([{"t1", "t2"}, {"t1", "t3"}])

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas tudo"))

        embed = env["canal"].sent[0]
        assert "Curtidas em comum" in embed.title
        assert "1 em comum" in embed.fields[0].name
        assert "Comum 1" in embed.fields[0].value
        assert "Comum 2" not in embed.fields[0].value

    async def test_conta_quem_salvou_sozinho(self, env):
        await com_escutas_em_comum(env)
        env["respostas"]["library"] = biblioteca([{"t1", "t2"}, {"t1", "t3"}])

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas tudo"))

        campo = next(f for f in env["canal"].sent[0].fields if f.name == "Salvou só um")
        assert "Otávio: **1**" in campo.value
        assert "Namorada: **1**" in campo.value

    async def test_ouviram_junto_mas_ninguem_salvou(self, env):
        await com_escutas_em_comum(env)
        env["respostas"]["library"] = biblioteca([set(), set()])

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas tudo"))

        embed = env["canal"].sent[0]
        assert "Nenhuma ainda" in embed.fields[0].name
        assert "nenhuma está salva" in embed.fields[0].value

    async def test_sem_escutas_em_comum(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Gi")
        await env["store"].record_plays([play(USER_A, "so-dele", "Só dele")])

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas tudo"))

        assert "nenhuma música em comum" in env["canal"].sent[0].description

    async def test_uma_conta_so(self, env):
        await conectar(env["store"], USER_A, "Otávio")

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas"))

        assert "duas contas" in env["canal"].sent[0].description

    async def test_escopo_antigo_pede_reconexao_sem_chamar_a_api(self, env):
        """Quem conectou antes do comando existir não concedeu user-library-read."""
        await conectar(env["store"], USER_A, "Otávio", scope="user-top-read")
        await conectar(env["store"], USER_B, "Gi")

        def nao_deveria_chamar(request):
            raise AssertionError("não pode consultar a biblioteca sem o escopo")

        env["respostas"]["library"] = nao_deveria_chamar

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas"))

        embed = env["canal"].sent[0]
        assert "Falta permissão" in embed.title
        assert "user-library-read" in embed.description
        assert "só ler" in embed.description

    async def test_periodo_invalido(self, env):
        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas decada"))
        assert "inválido" in env["canal"].sent[0].title

    async def test_403_mostra_a_causa_certa(self, env):
        await com_escutas_em_comum(env)
        env["respostas"]["library"] = httpx.Response(
            403,
            json={"error": {"status": 403, "message": "User not registered in the Developer Dashboard"}},
        )

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas tudo"))

        assert "não liberada" in env["canal"].sent[0].title

    async def test_terceiro_bloqueado(self, env):
        await env["listener"].handle_message(
            env["discord"], mensagem(env, "!curtidas", autor_id=333333333333333333)
        )
        assert "privado" in env["canal"].sent[0].title.lower()


class TestLotes:
    async def test_mais_de_40_faixas_vira_varias_chamadas(self, env):
        """O endpoint aceita no máximo 40 URIs por chamada."""
        await conectar(env["store"], USER_A, "Otávio")
        await conectar(env["store"], USER_B, "Gi")

        escutas = []
        for i in range(50):
            escutas.append(play(USER_A, f"t{i}", f"M{i}", i))
            escutas.append(play(USER_B, f"t{i}", f"M{i}", i))
        await env["store"].record_plays(escutas)

        tamanhos = []

        def handler(request):
            uris = request.url.params.get("uris", "").split(",")
            tamanhos.append(len(uris))
            return httpx.Response(200, json=[False] * len(uris))

        env["respostas"]["library"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas tudo"))

        assert max(tamanhos) <= 40, "nenhum lote pode passar de 40 URIs"
        assert sum(tamanhos) == 100  # 50 faixas × 2 pessoas


class TestBloqueio:
    async def test_curtidas_durante_bloqueio_nao_chama_e_diz_quando_volta(self, env):
        await com_escutas_em_comum(env)
        await env["listener"]._api.guard.block(33715, "QUOTA_EXCEEDED")
        chamadas = []

        def handler(request):
            chamadas.append(1)
            return httpx.Response(200, json=[])

        env["respostas"]["library"] = handler

        await env["listener"].handle_message(env["discord"], mensagem(env, "!curtidas tudo"))

        assert chamadas == []
        assert "em pausa" in env["canal"].sent[0].title
