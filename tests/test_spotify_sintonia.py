"""Aviso de que as duas contas estão ouvindo a mesma coisa ao mesmo tempo."""

import time

import httpx

from bot.spotify_listener import SYNC_PREFIX

# `env` é a fixture compartilhada com os testes do listener — importada de propósito.
from tests.test_spotify_listener import (  # noqa: F401
    USER_A, USER_B, conectar, env, mensagem, track_payload,
)


def sequencia(*respostas):
    """Devolve uma resposta diferente a cada chamada, para dar a cada conta a sua."""
    fila = list(respostas)

    def handler(request):
        return fila.pop(0) if fila else respostas[-1]

    return handler


def tocando(track_id, nome, artista="Artista", is_playing=True):
    payload = track_payload(track_id, nome, artista, is_playing=is_playing)
    payload["progress_ms"] = 30_000
    return httpx.Response(200, json=payload)


async def conectar_dois(env):
    await conectar(env["store"], USER_A, "Otávio")
    await conectar(env["store"], USER_B, "Gi")


class TestMesmaFaixa:
    async def test_anuncia_quando_os_dois_ouvem_a_mesma_faixa(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema")
        )

        await env["listener"].refresh_panel()

        avisos = [e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia"]
        assert len(avisos) == 1
        assert "Cinema" in avisos[0].description
        # os nomes vêm do display_name no Discord, não do perfil do Spotify
        assert "Otávio e Namorada" in avisos[0].description

    async def test_nao_menciona_os_usuarios(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema")
        )

        await env["listener"].refresh_panel()

        aviso = next(e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia")
        assert "<@" not in aviso.description

    async def test_nao_repete_o_aviso_da_mesma_faixa(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema"),
            tocando("t1", "Cinema"), tocando("t1", "Cinema"),
        )

        await env["listener"].refresh_panel()
        env["listener"]._panel_fingerprint = None
        await env["listener"].refresh_panel()

        avisos = [e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia"]
        assert len(avisos) == 1

    async def test_volta_a_anunciar_depois_do_cooldown(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema")
        )
        await env["listener"].refresh_panel()

        # envelhece a marca para além da janela
        antigo = int(time.time() * 1000) - 4 * 60 * 60 * 1000
        await env["store"].set_value(f"{SYNC_PREFIX}track:Cinema", str(antigo))

        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema")
        )
        env["listener"]._panel_fingerprint = None
        await env["listener"].refresh_panel()

        avisos = [e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia"]
        assert len(avisos) == 2

    async def test_marca_sobrevive_ao_restart(self, env):
        """A trava fica no banco, então reiniciar não faz o bot repetir o aviso."""
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema")
        )
        await env["listener"].refresh_panel()

        # simula restart: estado em memória some, banco fica
        env["listener"]._panel_fingerprint = None
        env["listener"]._panel_message = None
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema")
        )
        await env["listener"].refresh_panel()

        avisos = [e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia"]
        assert len(avisos) == 1


class TestQuandoNaoAnuncia:
    async def test_um_pausado_nao_conta(self, env):
        """A graça é os dois ouvindo de verdade no mesmo momento."""
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), tocando("t1", "Cinema", is_playing=False)
        )

        await env["listener"].refresh_panel()

        assert not [e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia"]

    async def test_faixas_e_artistas_diferentes(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema", "Harry Styles"),
            tocando("t2", "Outra", "SZA"),
        )

        await env["listener"].refresh_panel()

        titulos = [getattr(e, "title", "") for e in env["canal"].sent]
        assert "🎧 Sintonia" not in titulos
        assert "🎤 Mesmo artista" not in titulos

    async def test_so_uma_conta_conectada(self, env):
        await conectar(env["store"], USER_A, "Otávio")
        env["respostas"]["currently_playing"] = tocando("t1", "Cinema")

        await env["listener"].refresh_panel()

        assert not [e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia"]

    async def test_um_sem_reproducao_ativa(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema"), httpx.Response(204)
        )

        await env["listener"].refresh_panel()

        assert not [e for e in env["canal"].sent if getattr(e, "title", "") == "🎧 Sintonia"]


class TestMesmoArtista:
    async def test_anuncia_artista_quando_as_faixas_diferem(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema", "Harry Styles"),
            tocando("t2", "As It Was", "Harry Styles"),
        )

        await env["listener"].refresh_panel()

        avisos = [e for e in env["canal"].sent if getattr(e, "title", "") == "🎤 Mesmo artista"]
        assert len(avisos) == 1
        assert "Harry Styles" in avisos[0].description
        assert "faixas diferentes" in avisos[0].description

    async def test_album_inteiro_nao_vira_um_aviso_por_faixa(self, env):
        """Cooldown longo do artista: ouvir o álbum junto avisa uma vez, não dez."""
        await conectar_dois(env)

        for i in range(3):
            env["respostas"]["currently_playing"] = sequencia(
                tocando(f"a{i}", f"Faixa {i}", "Harry Styles"),
                tocando(f"b{i}", f"Outra {i}", "Harry Styles"),
            )
            env["listener"]._panel_fingerprint = None
            await env["listener"].refresh_panel()

        avisos = [e for e in env["canal"].sent if getattr(e, "title", "") == "🎤 Mesmo artista"]
        assert len(avisos) == 1

    async def test_mesma_faixa_tem_prioridade_sobre_mesmo_artista(self, env):
        await conectar_dois(env)
        env["respostas"]["currently_playing"] = sequencia(
            tocando("t1", "Cinema", "Harry Styles"),
            tocando("t1", "Cinema", "Harry Styles"),
        )

        await env["listener"].refresh_panel()

        titulos = [getattr(e, "title", "") for e in env["canal"].sent]
        assert "🎧 Sintonia" in titulos
        assert "🎤 Mesmo artista" not in titulos
