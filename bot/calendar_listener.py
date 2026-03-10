import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import discord

from .calendar_auth import CalendarAuth
from .calendar_client import CalendarClient


class CalendarListener:
    def __init__(
        self,
        auth: CalendarAuth,
        calendar_client: CalendarClient,
        channel_id: int,
        oauth_port: int = 8080,
    ) -> None:
        self._auth = auth
        self._calendar = calendar_client
        self._channel_id = channel_id
        self._oauth_port = oauth_port
        self._logger = logging.getLogger(__name__)
        self._active_sessions: Set[Tuple[int, int]] = set()

    @property
    def channel_id(self) -> int:
        return self._channel_id

    async def handle_message(self, client: discord.Client, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.channel.id != self._channel_id:
            return

        content = message.content.strip().lower()

        if content in ("!calendario-auth", "!eventos", "!agendar", "!cancelar"):
            self._logger.info(
                "Calendar command received",
                extra={
                    "context": {
                        "command": content,
                        "user_id": str(message.author.id),
                        "user_name": str(message.author),
                    }
                },
            )

        if content == "!calendario-auth":
            await self._handle_auth(message)
        elif content == "!eventos":
            await self._handle_list_events(message)
        elif content == "!agendar":
            await self._handle_create_event(client, message)
        elif content == "!cancelar":
            await self._handle_cancel_event(client, message)

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    async def _handle_auth(self, message: discord.Message) -> None:
        if not isinstance(message.author, discord.Member) or not message.author.guild_permissions.administrator:
            self._logger.warning(
                "Non-admin user attempted !calendario-auth",
                extra={"context": {"user_id": str(message.author.id), "user_name": str(message.author)}},
            )
            await message.channel.send(
                f"{message.author.mention} apenas administradores podem usar `!calendario-auth`."
            )
            return

        self._logger.info(
            "Starting OAuth flow",
            extra={"context": {"user_id": str(message.author.id), "oauth_port": self._oauth_port}},
        )
        auth_url = self._auth.get_auth_url()
        await message.channel.send(
            f"{message.author.mention} acesse o link abaixo para autorizar o Google Calendar:\n"
            f"<{auth_url}>\n\n"
            f"Aguardando autorizacao (5 minutos)..."
        )

        self._logger.info("Waiting for OAuth callback", extra={"context": {"port": self._oauth_port}})
        code = await self._auth.wait_for_callback(port=self._oauth_port, timeout=300.0)
        if code is None:
            self._logger.warning("OAuth flow timed out, no code received")
            await message.channel.send(
                f"{message.author.mention} tempo esgotado. Use `!calendario-auth` novamente."
            )
            return

        try:
            await self._auth.exchange_code(code)
            self._logger.info("OAuth flow completed successfully")
            await message.channel.send(
                f"{message.author.mention} Google Calendar autorizado com sucesso!"
            )
        except Exception as exc:
            self._logger.error(
                "Failed to exchange OAuth code",
                extra={"context": {"error": str(exc)}},
            )
            await message.channel.send(
                f"{message.author.mention} falha ao concluir autorizacao. Tente novamente."
            )

    # ------------------------------------------------------------------ #
    # List events
    # ------------------------------------------------------------------ #

    async def _handle_list_events(self, message: discord.Message) -> None:
        if not self._auth.is_authenticated():
            self._logger.warning("!eventos called but bot is not authenticated")
            await message.channel.send(
                f"{message.author.mention} o bot nao esta autenticado no Google Calendar. "
                "Um administrador deve usar `!calendario-auth` primeiro."
            )
            return

        self._logger.info("Fetching calendar events for next 7 days")
        try:
            events = await self._calendar.list_events(days=7)
        except Exception as exc:
            self._logger.error(
                "Failed to list calendar events",
                extra={"context": {"error": str(exc)}},
            )
            await message.channel.send(f"{message.author.mention} falha ao buscar eventos do Calendar.")
            return

        self._logger.info("Calendar events fetched", extra={"context": {"count": len(events)}})
        if not events:
            await message.channel.send(
                f"{message.author.mention} nenhum evento nos proximos 7 dias."
            )
            return

        lines = [f"{message.author.mention} proximos eventos (7 dias):"]
        for i, event in enumerate(events, start=1):
            start = event["start"].get("dateTime", event["start"].get("date", "?"))
            title = event.get("summary", "(sem titulo)")
            lines.append(f"{i}. **{title}** — {_format_datetime(start)}")

        await message.channel.send("\n".join(lines))

    # ------------------------------------------------------------------ #
    # Create event
    # ------------------------------------------------------------------ #

    async def _handle_create_event(self, client: discord.Client, message: discord.Message) -> None:
        if not self._auth.is_authenticated():
            await message.channel.send(
                f"{message.author.mention} o bot nao esta autenticado no Google Calendar. "
                "Um administrador deve usar `!calendario-auth` primeiro."
            )
            return

        session_key = (message.channel.id, message.author.id)
        if session_key in self._active_sessions:
            await message.channel.send(
                f"{message.author.mention} voce ja tem uma sessao em andamento."
            )
            return

        self._active_sessions.add(session_key)
        try:
            await self._run_create_event_flow(client, message)
        finally:
            self._active_sessions.discard(session_key)

    async def _run_create_event_flow(self, client: discord.Client, message: discord.Message) -> None:
        channel = message.channel
        author = message.author

        def check(m: discord.Message) -> bool:
            return m.channel.id == channel.id and m.author.id == author.id

        # Titulo
        await channel.send(f"{author.mention} qual o titulo do evento?")
        try:
            title_msg = await client.wait_for("message", check=check, timeout=60.0)
        except asyncio.TimeoutError:
            await channel.send(f"{author.mention} tempo esgotado. Use `!agendar` novamente.")
            return
        title = title_msg.content.strip()

        # Data e hora
        await channel.send(
            f"{author.mention} qual a data e hora?\n"
            "Formatos aceitos: `DD/MM/AAAA HH:MM` ou `AAAA-MM-DD HH:MM`"
        )
        start_dt: Optional[datetime] = None
        while start_dt is None:
            try:
                dt_msg = await client.wait_for("message", check=check, timeout=60.0)
            except asyncio.TimeoutError:
                await channel.send(f"{author.mention} tempo esgotado. Use `!agendar` novamente.")
                return
            start_dt = _parse_datetime(dt_msg.content.strip())
            if start_dt is None:
                await channel.send(
                    f"{author.mention} formato invalido. Use `DD/MM/AAAA HH:MM` ou `AAAA-MM-DD HH:MM`."
                )

        # Duracao
        await channel.send(f"{author.mention} duracao em minutos? (padrao: 60)")
        duration = 60
        try:
            dur_msg = await client.wait_for("message", check=check, timeout=30.0)
            content = dur_msg.content.strip()
            if content.isdigit() and int(content) > 0:
                duration = int(content)
        except asyncio.TimeoutError:
            pass  # usa o padrao

        self._logger.info(
            "Creating calendar event",
            extra={"context": {"title": title, "start_dt": str(start_dt), "duration_minutes": duration}},
        )
        try:
            event = await self._calendar.create_event(
                title=title,
                start_dt=start_dt,
                duration_minutes=duration,
            )
            event_link = event.get("htmlLink", "")
            formatted_start = _format_datetime(event["start"].get("dateTime", ""))
            self._logger.info(
                "Calendar event created",
                extra={"context": {"event_id": event.get("id"), "title": title}},
            )
            await channel.send(
                f"{author.mention} evento **{title}** criado para {formatted_start}.\n{event_link}"
            )
        except Exception as exc:
            self._logger.error(
                "Failed to create calendar event",
                extra={"context": {"error": str(exc)}},
            )
            await channel.send(f"{author.mention} falha ao criar o evento.")

    # ------------------------------------------------------------------ #
    # Cancel event
    # ------------------------------------------------------------------ #

    async def _handle_cancel_event(self, client: discord.Client, message: discord.Message) -> None:
        if not self._auth.is_authenticated():
            await message.channel.send(
                f"{message.author.mention} o bot nao esta autenticado no Google Calendar. "
                "Um administrador deve usar `!calendario-auth` primeiro."
            )
            return

        session_key = (message.channel.id, message.author.id)
        if session_key in self._active_sessions:
            await message.channel.send(
                f"{message.author.mention} voce ja tem uma sessao em andamento."
            )
            return

        self._active_sessions.add(session_key)
        try:
            await self._run_cancel_event_flow(client, message)
        finally:
            self._active_sessions.discard(session_key)

    async def _run_cancel_event_flow(self, client: discord.Client, message: discord.Message) -> None:
        channel = message.channel
        author = message.author

        try:
            events = await self._calendar.list_events(days=30)
        except Exception as exc:
            self._logger.error(
                "Failed to list events for cancel",
                extra={"context": {"error": str(exc)}},
            )
            await channel.send(f"{author.mention} falha ao buscar eventos.")
            return

        if not events:
            await channel.send(f"{author.mention} nenhum evento encontrado nos proximos 30 dias.")
            return

        lines = [f"{author.mention} qual evento deseja cancelar?"]
        for i, event in enumerate(events, start=1):
            start = event["start"].get("dateTime", event["start"].get("date", "?"))
            title = event.get("summary", "(sem titulo)")
            lines.append(f"{i}. **{title}** — {_format_datetime(start)}")
        await channel.send("\n".join(lines))

        def check(m: discord.Message) -> bool:
            return m.channel.id == channel.id and m.author.id == author.id

        selected: Optional[Dict[str, Any]] = None
        while selected is None:
            try:
                resp = await client.wait_for("message", check=check, timeout=60.0)
            except asyncio.TimeoutError:
                await channel.send(f"{author.mention} tempo esgotado. Use `!cancelar` novamente.")
                return
            content = resp.content.strip()
            if content.isdigit():
                idx = int(content)
                if 1 <= idx <= len(events):
                    selected = events[idx - 1]
                    break
            await channel.send(f"{author.mention} escolha um numero entre 1 e {len(events)}.")

        self._logger.info(
            "Deleting calendar event",
            extra={"context": {"event_id": selected["id"], "title": selected.get("summary")}},
        )
        try:
            await self._calendar.delete_event(selected["id"])
            self._logger.info("Calendar event deleted", extra={"context": {"event_id": selected["id"]}})
            await channel.send(
                f"{author.mention} evento **{selected.get('summary', '?')}** cancelado com sucesso."
            )
        except Exception as exc:
            self._logger.error(
                "Failed to delete calendar event",
                extra={"context": {"error": str(exc)}},
            )
            await channel.send(f"{author.mention} falha ao cancelar o evento.")


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _parse_datetime(text: str) -> Optional[datetime]:
    formats = [
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _format_datetime(dt_string: str) -> str:
    if not dt_string:
        return "?"
    try:
        dt = datetime.fromisoformat(dt_string)
        return dt.strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return dt_string
