"""Ponte para o Jarvis: repassa as DMs do Otávio ao jarvis-central.

O Jarvis usa este bot para mandar a reserva quando o WhatsApp cai, mas não lê o
Discord. Sem esta ponte, com o WhatsApp fora, o Otávio não tinha como responder.
O central responde pelo próprio bot, via API REST — esta ponte só leva a ida.

Só a DM do dono, e só o que não for comando "!" (os comandos continuam do bot).
"""

import logging
from typing import Any, Awaitable, Callable, Optional

import httpx

Postar = Callable[[str, dict, dict], Awaitable[int]]


async def _postar_httpx(url: str, corpo: dict, cabecalhos: dict) -> int:
    # TLS verificado: o central é público e recebe um segredo no cabeçalho.
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), verify=True) as client:
        resp = await client.post(url, json=corpo, headers=cabecalhos)
        return resp.status_code


class JarvisPonte:
    def __init__(self, url_base: str, segredo: str, dono_id: int, postar: Optional[Postar] = None) -> None:
        self._url = url_base.rstrip("/") + "/discord/mensagem"
        self._segredo = segredo
        self._dono_id = dono_id
        self._postar = postar or _postar_httpx
        self._logger = logging.getLogger(__name__)

    def deve_encaminhar(self, message: Any, em_dm: bool) -> bool:
        conteudo = (getattr(message, "content", "") or "").strip()
        return em_dm and message.author.id == self._dono_id and bool(conteudo) and not conteudo.startswith("!")

    async def encaminhar(self, message: Any) -> bool:
        corpo = {
            "autorId": str(message.author.id),
            "mensagemId": str(message.id),
            "texto": message.content.strip()[:4000],
        }
        cabecalhos = {"x-jarvis-discord": self._segredo}
        # Uma retentativa: o id da mensagem deduplica no central, então reenviar é seguro.
        for tentativa in (1, 2):
            try:
                status = await self._postar(self._url, corpo, cabecalhos)
                if 200 <= status < 300:
                    return True
                self._logger.warning("Jarvis recusou a DM", extra={"context": {"status": status, "tentativa": tentativa}})
                if status < 500:
                    return False
            except httpx.HTTPError as exc:
                self._logger.warning("Jarvis indisponível", extra={"context": {"erro": type(exc).__name__, "tentativa": tentativa}})
        return False
