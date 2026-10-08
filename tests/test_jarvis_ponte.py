"""A ponte leva ao Jarvis só a DM do dono que não é comando."""

from types import SimpleNamespace

import httpx

from bot.jarvis_ponte import JarvisPonte

DONO = 42


def _msg(texto: str, autor: int = DONO, mid: int = 7):
    return SimpleNamespace(content=texto, id=mid, author=SimpleNamespace(id=autor))


def _ponte(respostas):
    chamadas = []

    async def postar(url, corpo, cabecalhos):
        chamadas.append((url, corpo, cabecalhos))
        r = respostas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    return JarvisPonte("https://jarvis.exemplo/", "s" * 32, DONO, postar), chamadas


def test_so_dm_do_dono_que_nao_e_comando():
    ponte, _ = _ponte([])
    assert ponte.deve_encaminhar(_msg("fui pra academia"), em_dm=True)
    assert not ponte.deve_encaminhar(_msg("fui pra academia"), em_dm=False), "canal de servidor fica com o bot"
    assert not ponte.deve_encaminhar(_msg("oi", autor=99), em_dm=True), "outra pessoa"
    assert not ponte.deve_encaminhar(_msg("!tasks"), em_dm=True), "comando continua do bot"
    assert not ponte.deve_encaminhar(_msg("   "), em_dm=True)


async def test_envia_com_segredo_e_ids():
    ponte, chamadas = _ponte([202])
    assert await ponte.encaminhar(_msg(" status ", mid=123))
    url, corpo, cabecalhos = chamadas[0]
    assert url == "https://jarvis.exemplo/discord/mensagem"
    assert corpo == {"autorId": "42", "mensagemId": "123", "texto": "status"}
    assert cabecalhos == {"x-jarvis-discord": "s" * 32}


async def test_tenta_de_novo_em_erro_de_rede_ou_5xx_mas_nao_em_4xx():
    ponte, chamadas = _ponte([httpx.ConnectError("fora"), 202])
    assert await ponte.encaminhar(_msg("oi"))
    assert len(chamadas) == 2

    ponte, chamadas = _ponte([503, 503])
    assert not await ponte.encaminhar(_msg("oi"))
    assert len(chamadas) == 2

    ponte, chamadas = _ponte([401])
    assert not await ponte.encaminhar(_msg("oi"))
    assert len(chamadas) == 1
