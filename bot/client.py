import asyncio
import datetime as dt
import logging
from datetime import datetime, timezone
from typing import Optional, Set
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks

from .calendar_listener import CalendarListener
from .julgar_listener import JulgarListener
from .notion_client import NotionClient
from .shift_manager import (
    calculate_summary,
    format_duration,
    build_history_line,
    is_shift_open,
    now_local,
    now_timestamp,
    parse_shift_page,
    serialize_entries,
)
from .shift_views import ShiftEditView
from .task_views import StatusSelectView, StopTimerSelectView
from .timer_manager import TimerManager
from .voice_listener import VoiceListener

STATUS_INDICATORS = {
    "Not started": "⬜",
    "In progress": "🔵",
    "Done": "✅",
}
DEFAULT_STATUS_INDICATOR = "⚪"

def _build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📖 Comandos disponíveis",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="📋 Tarefas",
        value=(
            "`!tasks` — Lista suas tarefas do Notion\n"
            "`!create-task` — Cria uma nova tarefa\n"
        ),
        inline=False,
    )
    embed.add_field(
        name="⏱️ Cronômetro",
        value="`!stop-timer` — Para um cronômetro ativo",
        inline=False,
    )
    embed.add_field(
        name="🕐 Turnos",
        value=(
            "`!shift` — Registra entrada/saída (alterna automático)\n"
            "`!shifts` — Lista turnos recentes com resumo\n"
            "`!shift-edit` — Editar último turno"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Config",
        value=(
            "`!logs on` — Ativa logs de comandos no DM\n"
            "`!logs off` — Desativa logs"
        ),
        inline=False,
    )
    embed.set_footer(text="Envie qualquer comando para começar")
    return embed


def _embed_info(description: str) -> discord.Embed:
    return discord.Embed(description=description, color=discord.Color.blurple())


def _embed_success(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.green())


def _embed_error(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _embed_warning(description: str) -> discord.Embed:
    return discord.Embed(description=description, color=discord.Color.orange())


class VoiceWatcherClient(discord.Client):
    def __init__(
        self,
        voice_listener: VoiceListener,
        julgar_listener: JulgarListener,
        notion_client: Optional[NotionClient] = None,
        timer_manager: Optional[TimerManager] = None,
        calendar_listener: Optional[CalendarListener] = None,
        target_user_id: Optional[int] = None,
        tz_name: str = "America/Sao_Paulo",
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.messages = True
        intents.dm_messages = True
        intents.message_content = True
        intents.members = True

        super().__init__(intents=intents)
        self._logger = logging.getLogger(__name__)
        self._voice_listener = voice_listener
        self._julgar_listener = julgar_listener
        self._notion_client = notion_client
        self._timer_manager = timer_manager or TimerManager()
        self._calendar_listener = calendar_listener
        self._target_user_id = target_user_id
        self._tz_name = tz_name
        self._tz = ZoneInfo(tz_name)
        self._status_options_cache: Optional[list] = None
        self._dm_log_subscribers: Set[int] = set()

        self._daily_reminders = tasks.loop(time=[
            dt.time(hour=9, minute=0, tzinfo=self._tz),
            dt.time(hour=13, minute=15, tzinfo=self._tz),
            dt.time(hour=14, minute=15, tzinfo=self._tz),
        ])(self._on_daily_reminder)
        self._daily_reminders.before_loop(self._wait_until_ready)

    async def _get_status_options(self) -> list:
        if self._status_options_cache:
            return self._status_options_cache
        if self._notion_client:
            try:
                self._status_options_cache = await self._notion_client.fetch_status_options()
            except Exception:
                self._status_options_cache = ["Not started", "In progress", "Done"]
        else:
            self._status_options_cache = ["Not started", "In progress", "Done"]
        return self._status_options_cache

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def on_ready(self) -> None:
        user_display = f"{self.user} ({self.user.id})" if self.user else "unknown"
        self._logger.info(
            "Discord bot connected to gateway",
            extra={"context": {"bot_user": user_display}},
        )
        await self._log_monitored_channel_status()
        await self._log_julgar_channel_status()

        if not self._daily_reminders.is_running():
            self._daily_reminders.start()

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        self._logger.debug(
            "Voice state update received",
            extra={
                "context": {
                    "user_id": str(member.id),
                    "before_channel_id": str(before.channel.id) if before.channel else None,
                    "after_channel_id": str(after.channel.id) if after.channel else None,
                }
            },
        )
        await self._voice_listener.handle_voice_state_update(member, before, after)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return

        if isinstance(message.channel, discord.DMChannel):
            await self._handle_dm_command(message)
            return

        await self._julgar_listener.handle_message(self, message)
        if self._calendar_listener is not None:
            await self._calendar_listener.handle_message(self, message)

    # ------------------------------------------------------------------
    # DM command router
    # ------------------------------------------------------------------

    async def _handle_dm_command(self, message: discord.Message) -> None:
        content = message.content.strip()
        cmd = content.lower()

        if cmd == "!help":
            await message.channel.send(embed=_build_help_embed())
        elif cmd == "!tasks":
            await self._notify_dm_log(message, "!tasks")
            await self._handle_tasks_dm(message)
        elif cmd == "!create-task":
            await self._notify_dm_log(message, "!create-task")
            await self._handle_create_task_dm(message)
        elif cmd == "!stop-timer":
            await self._notify_dm_log(message, "!stop-timer")
            await self._handle_stop_timer_dm(message)
        elif cmd == "!shift":
            await self._notify_dm_log(message, "!shift")
            await self._handle_shift(message)
        elif cmd == "!shifts":
            await self._notify_dm_log(message, "!shifts")
            await self._handle_shifts(message)
        elif cmd == "!shift-edit":
            await self._notify_dm_log(message, "!shift-edit")
            await self._handle_shift_edit(message)
        elif cmd == "!logs on":
            self._dm_log_subscribers.add(message.author.id)
            await message.channel.send(embed=_embed_success("✅ Logs ativados", "Você receberá notificações de comandos no DM."))
        elif cmd == "!logs off":
            self._dm_log_subscribers.discard(message.author.id)
            await message.channel.send(embed=_embed_info("Logs desativados."))

    # ------------------------------------------------------------------
    # !tasks
    # ------------------------------------------------------------------

    async def _handle_tasks_dm(self, message: discord.Message) -> None:
        if not self._notion_client:
            await message.channel.send(embed=_embed_error("❌ Notion não configurado", "Defina `NOTION_TOKEN` e `NOTION_DATABASE_ID`."))
            return

        try:
            tasks = await self._notion_client.fetch_tasks()
        except Exception as exc:
            self._logger.error("Failed to fetch Notion tasks", extra={"context": {"error": str(exc)}})
            await message.channel.send(embed=_embed_error("❌ Erro ao buscar tarefas", f"```{exc}```"))
            return

        if not tasks:
            await message.channel.send(embed=_embed_info("Nenhuma tarefa encontrada no banco de dados."))
            return

        embeds = self._build_task_embeds(tasks)
        for i in range(0, len(embeds), 10):
            await message.channel.send(embeds=embeds[i : i + 10])

    # ------------------------------------------------------------------
    # !create-task (conversational flow)
    # ------------------------------------------------------------------

    async def _handle_create_task_dm(self, message: discord.Message) -> None:
        if not self._notion_client:
            await message.channel.send(embed=_embed_error("❌ Notion não configurado", "Defina `NOTION_TOKEN` e `NOTION_DATABASE_ID`."))
            return

        channel = message.channel
        author = message.author

        def check(m: discord.Message) -> bool:
            return m.author.id == author.id and m.channel.id == channel.id

        prompt = discord.Embed(
            title="📝 Nova Tarefa",
            description="Qual o **nome** da tarefa?",
            color=discord.Color.blurple(),
        )
        prompt.set_footer(text="Responda com o nome ou 'cancelar' para desistir")
        await channel.send(embed=prompt)

        try:
            name_msg = await self.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await channel.send(embed=_embed_warning("⏰ Tempo esgotado. Use `!create-task` para tentar novamente."))
            return

        if name_msg.content.strip().lower() in ("cancelar", "cancel"):
            await channel.send(embed=_embed_info("Criação cancelada."))
            return

        task_name = name_msg.content.strip()

        prompt2 = discord.Embed(
            description="Quer adicionar uma **descrição**?\nDigite a descrição ou `não` para pular.",
            color=discord.Color.blurple(),
        )
        await channel.send(embed=prompt2)

        try:
            desc_msg = await self.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await channel.send(embed=_embed_warning("⏰ Tempo esgotado."))
            return

        desc_content = desc_msg.content.strip()
        task_description = None if desc_content.lower() in ("não", "nao", "no", "n") else desc_content

        status_options = await self._get_status_options()
        view = StatusSelectView(
            notion_client=self._notion_client,
            timer_manager=self._timer_manager,
            task_name=task_name,
            task_description=task_description,
            status_options=status_options,
        )

        status_embed = discord.Embed(
            title="📋 Escolha o status",
            description=f"Tarefa: **{task_name}**",
            color=discord.Color.blurple(),
        )
        await channel.send(embed=status_embed, view=view)

    # ------------------------------------------------------------------
    # !stop-timer
    # ------------------------------------------------------------------

    async def _handle_stop_timer_dm(self, message: discord.Message) -> None:
        if not self._notion_client:
            await message.channel.send(embed=_embed_error("❌ Notion não configurado"))
            return

        active = self._timer_manager.get_active(message.author.id)
        if not active:
            await message.channel.send(embed=_embed_info("Você não tem nenhum cronômetro ativo."))
            return

        status_options = await self._get_status_options()
        view = StopTimerSelectView(
            notion_client=self._notion_client,
            timer_manager=self._timer_manager,
            status_options=status_options,
            user_id=message.author.id,
        )

        embed = discord.Embed(
            title="⏱️ Cronômetros ativos",
            description=f"Você tem **{len(active)}** cronômetro(s) rodando.\nSelecione qual deseja parar:",
            color=discord.Color.orange(),
        )
        for t in active[:10]:
            embed.add_field(name=t.task_name, value=f"⏱️ `{t.elapsed_display}`", inline=True)

        await message.channel.send(embed=embed, view=view)

    # ------------------------------------------------------------------
    # !shift — toggle entry
    # ------------------------------------------------------------------

    async def _handle_shift(self, message: discord.Message) -> None:
        if not self._notion_client:
            await message.channel.send(embed=_embed_error("❌ Notion não configurado"))
            return

        try:
            raw_pages = await self._notion_client.fetch_shifts(limit=1)
        except Exception as exc:
            await message.channel.send(embed=_embed_error("❌ Erro ao buscar turnos", f"```{exc}```"))
            return

        now = now_timestamp(self._tz_name)
        today = now_local(self._tz_name).strftime("%Y-%m-%d")

        open_shift = None
        if raw_pages:
            shift = parse_shift_page(raw_pages[0])
            if shift["is_open"]:
                open_shift = shift

        if open_shift:
            entries = list(open_shift["entries"])
            entries.append(now)
            new_json = serialize_entries(entries)

            try:
                await self._notion_client.update_shift_entries(open_shift["id"], new_json)
            except Exception as exc:
                await message.channel.send(embed=_embed_error("❌ Erro ao atualizar turno", f"```{exc}```"))
                return

            still_open = is_shift_open(entries)
            summary = calculate_summary(entries, self._tz_name)

            if still_open:
                color = discord.Color.green()
                status_text = "🟢 Trabalhando"
            else:
                color = discord.Color.orange()
                status_text = "🟠 Pausa / Encerrado"

            embed = discord.Embed(
                title=f"Turno {open_shift['name']}",
                color=color,
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Status", value=status_text, inline=True)
            embed.add_field(name="Entrada", value=f"`{now}`", inline=True)
            embed.add_field(name="Histórico", value=build_history_line(entries), inline=False)
            embed.add_field(
                name="Trabalhado",
                value=f"`{format_duration(summary['total_work_min'])}`",
                inline=True,
            )
            if summary["pauses"]:
                pause_details = ", ".join(
                    f"{p[0]}-{p[1]} ({format_duration(p[2])})" for p in summary["pauses"]
                )
                embed.add_field(
                    name=f"Pausas ({len(summary['pauses'])})",
                    value=f"`{format_duration(summary['total_pause_min'])}` — {pause_details}",
                    inline=False,
                )
            embed.set_footer(text="Use !shift para registrar próxima entrada")
            await message.channel.send(embed=embed)

        else:
            first_entry = [now]
            entries_json = serialize_entries(first_entry)
            shift_start = now_local(self._tz_name).isoformat()

            try:
                page = await self._notion_client.create_shift(
                    name=today,
                    shift_start=shift_start,
                    entries_json=entries_json,
                )
            except Exception as exc:
                await message.channel.send(embed=_embed_error("❌ Erro ao criar turno", f"```{exc}```"))
                return

            embed = discord.Embed(
                title=f"Turno {today}",
                description="Novo turno iniciado!",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(name="Status", value="🟢 Trabalhando", inline=True)
            embed.add_field(name="Entrada", value=f"`{now}`", inline=True)
            embed.set_footer(text="Use !shift para registrar pausa ou saída")
            await message.channel.send(embed=embed)

    # ------------------------------------------------------------------
    # !shifts — list recent shifts
    # ------------------------------------------------------------------

    async def _handle_shifts(self, message: discord.Message) -> None:
        if not self._notion_client:
            await message.channel.send(embed=_embed_error("❌ Notion não configurado"))
            return

        try:
            raw_pages = await self._notion_client.fetch_shifts(limit=10)
        except Exception as exc:
            await message.channel.send(embed=_embed_error("❌ Erro ao buscar turnos", f"```{exc}```"))
            return

        if not raw_pages:
            await message.channel.send(embed=_embed_info("Nenhum turno registrado ainda."))
            return

        shifts = [parse_shift_page(p) for p in raw_pages]

        lines = []
        for s in shifts:
            summary = calculate_summary(s["entries"], self._tz_name)
            status = "🟢 Aberto" if s["is_open"] else "⚪ Fechado"
            work = format_duration(summary["total_work_min"])
            pause_count = len(summary["pauses"])
            pause_text = f"{pause_count} pausa(s)" if pause_count else "0 pausas"
            if summary["total_pause_min"]:
                pause_text += f" ({format_duration(summary['total_pause_min'])})"
            if s["is_open"]:
                work += " (parcial)"
            lines.append(f"**{s['name']}** — `{work}` · {pause_text} · {status}")

        embed = discord.Embed(
            title=f"📊 Turnos recentes ({len(shifts)})",
            description="\n\n".join(lines),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        await message.channel.send(embed=embed)

    # ------------------------------------------------------------------
    # !shift-edit — edit current/last shift
    # ------------------------------------------------------------------

    async def _handle_shift_edit(self, message: discord.Message) -> None:
        if not self._notion_client:
            await message.channel.send(embed=_embed_error("❌ Notion não configurado"))
            return

        try:
            raw_pages = await self._notion_client.fetch_shifts(limit=1)
        except Exception as exc:
            await message.channel.send(embed=_embed_error("❌ Erro ao buscar turnos", f"```{exc}```"))
            return

        if not raw_pages:
            await message.channel.send(embed=_embed_info("Nenhum turno encontrado para editar."))
            return

        shift = parse_shift_page(raw_pages[0])
        summary = calculate_summary(shift["entries"], self._tz_name)
        status = "🟢 Aberto" if shift["is_open"] else "⚪ Fechado"

        embed = discord.Embed(
            title=f"✏️ Editar turno {shift['name']}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Trabalhado", value=f"`{format_duration(summary['total_work_min'])}`", inline=True)
        embed.add_field(name="Histórico", value=build_history_line(shift["entries"]) or "-", inline=False)

        view = ShiftEditView(
            notion_client=self._notion_client,
            shift=shift,
        )
        await message.channel.send(embed=embed, view=view)

    # ------------------------------------------------------------------
    # Scheduled daily reminders
    # ------------------------------------------------------------------

    async def _wait_until_ready(self) -> None:
        await self.wait_until_ready()

    async def _on_daily_reminder(self) -> None:
        local_now = datetime.now(self._tz)
        if local_now.weekday() >= 5:
            return

        if not self._target_user_id or not self._notion_client:
            return

        try:
            user = await self.fetch_user(self._target_user_id)
            dm = await user.create_dm()
        except Exception as exc:
            self._logger.error("Failed to open DM for scheduled reminder", extra={"context": {"error": str(exc)}})
            return

        hour, minute = local_now.hour, local_now.minute

        if hour == 9 and minute < 5:
            await self._send_morning_reminder(dm)
        elif hour == 13 and 15 <= minute < 20:
            await self._send_lunch_out_reminder(dm)
        elif hour == 14 and 15 <= minute < 20:
            await self._send_lunch_back_reminder(dm)

    async def _send_morning_reminder(self, dm: discord.DMChannel) -> None:
        embed = discord.Embed(
            title="☀️ Bom dia!",
            description="Hora de bater o ponto. Use `!shift` para registrar entrada.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )

        try:
            task_list = await self._notion_client.fetch_tasks()
        except Exception:
            task_list = []

        if task_list:
            status_counts: dict[str, int] = {}
            for t in task_list:
                st = t.get("property_status") or "N/A"
                status_counts[st] = status_counts.get(st, 0) + 1

            summary_lines = []
            for st, count in status_counts.items():
                indicator = STATUS_INDICATORS.get(st, DEFAULT_STATUS_INDICATOR)
                summary_lines.append(f"{indicator} **{st}**: {count}")

            embed.add_field(
                name=f"📊 Resumo de tarefas ({len(task_list)})",
                value="\n".join(summary_lines),
                inline=False,
            )

            active = [t for t in task_list if t.get("property_status") in ("In progress", "Em andamento")]
            if active:
                lines = []
                for t in active[:5]:
                    due = f" · Prazo: `{t['property_due']}`" if t.get("property_due") else ""
                    lines.append(f"• **[{t['name']}]({t['url']})**{due}")
                embed.add_field(
                    name="🔵 Em andamento",
                    value="\n".join(lines),
                    inline=False,
                )

        embed.set_footer(text="Responda !shift para registrar ponto | !tasks para ver todas")
        await dm.send(embed=embed)

    async def _send_lunch_out_reminder(self, dm: discord.DMChannel) -> None:
        embed = discord.Embed(
            title="🍽️ Hora do almoço!",
            description="Lembre de bater o ponto de saída.\nUse `!shift` para registrar.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Bom apetite!")
        await dm.send(embed=embed)

    async def _send_lunch_back_reminder(self, dm: discord.DMChannel) -> None:
        embed = discord.Embed(
            title="⏰ Hora de voltar!",
            description="Lembre de bater o ponto de entrada.\nUse `!shift` para registrar.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text="Bom trabalho!")
        await dm.send(embed=embed)

    # ------------------------------------------------------------------
    # DM log notification
    # ------------------------------------------------------------------

    async def _notify_dm_log(self, message: discord.Message, command: str) -> None:
        if not self._dm_log_subscribers:
            return

        now = datetime.now(timezone.utc)
        channel_info = (
            "DM" if isinstance(message.channel, discord.DMChannel)
            else f"#{getattr(message.channel, 'name', '?')}"
        )

        embed = discord.Embed(
            title="📡 Comando recebido",
            color=discord.Color.light_grey(),
            timestamp=now,
        )
        embed.add_field(name="Comando", value=f"`{command}`", inline=True)
        embed.add_field(name="Canal", value=channel_info, inline=True)
        embed.add_field(name="Usuário", value=f"{message.author} (`{message.author.id}`)", inline=False)

        for uid in list(self._dm_log_subscribers):
            try:
                dm_user = self.get_user(uid) or await self.fetch_user(uid)
                dm_channel = dm_user.dm_channel or await dm_user.create_dm()
                await dm_channel.send(embed=embed)
            except Exception as exc:
                self._logger.warning(
                    "Failed to send DM log",
                    extra={"context": {"user_id": uid, "error": str(exc)}},
                )

    # ------------------------------------------------------------------
    # Embeds
    # ------------------------------------------------------------------

    def _build_task_embeds(self, tasks: list) -> list:
        chunk_size = 10
        embeds = []

        for i in range(0, len(tasks), chunk_size):
            chunk = tasks[i : i + chunk_size]
            lines = []
            for task in chunk:
                indicator = STATUS_INDICATORS.get(
                    task["property_status"], DEFAULT_STATUS_INDICATOR
                )
                line = f"{indicator} **[{task['name']}]({task['url']})**"
                line += f"\n    Status: `{task['property_status'] or 'N/A'}`"
                if task["property_due"]:
                    line += f" · Prazo: `{task['property_due']}`"
                if task["property_description"]:
                    desc = task["property_description"][:100]
                    line += f"\n    {desc}"
                lines.append(line)

            total = len(tasks)
            page = (i // chunk_size) + 1
            total_pages = (total + chunk_size - 1) // chunk_size
            title = f"📋 Tarefas Notion ({total})"
            if total_pages > 1:
                title += f" — página {page}/{total_pages}"

            embed = discord.Embed(
                title=title,
                description="\n\n".join(lines),
                color=discord.Color.blurple(),
            )
            embeds.append(embed)

        return embeds

    # ------------------------------------------------------------------
    # Startup channel validation
    # ------------------------------------------------------------------

    async def _log_monitored_channel_status(self) -> None:
        monitored_channel_id = self._voice_listener.voice_channel_id
        channel = self.get_channel(monitored_channel_id)

        if channel is None:
            try:
                channel = await self.fetch_channel(monitored_channel_id)
            except discord.NotFound:
                self._logger.error(
                    "Monitored channel not found",
                    extra={"context": {"channel_id": str(monitored_channel_id)}},
                )
                return
            except discord.Forbidden:
                self._logger.error(
                    "No permission to access monitored channel",
                    extra={"context": {"channel_id": str(monitored_channel_id)}},
                )
                return
            except discord.HTTPException as exc:
                self._logger.error(
                    "Failed to resolve monitored channel",
                    extra={
                        "context": {
                            "channel_id": str(monitored_channel_id),
                            "error": str(exc),
                        }
                    },
                )
                return

        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            self._logger.warning(
                "Configured monitored channel is not a voice channel",
                extra={
                    "context": {
                        "channel_id": str(monitored_channel_id),
                        "channel_type": str(channel.type),
                    }
                },
            )
            return

        self._logger.info(
            "Monitoring voice channel is active",
            extra={
                "context": {
                    "guild_id": str(channel.guild.id),
                    "guild_name": channel.guild.name,
                    "channel_id": str(channel.id),
                    "channel_name": channel.name,
                }
            },
        )

    async def _log_julgar_channel_status(self) -> None:
        monitored_channel_id = self._julgar_listener.text_channel_id
        channel = self.get_channel(monitored_channel_id)

        if channel is None:
            try:
                channel = await self.fetch_channel(monitored_channel_id)
            except discord.NotFound:
                self._logger.error(
                    "Julgar text channel not found",
                    extra={"context": {"channel_id": str(monitored_channel_id)}},
                )
                return
            except discord.Forbidden:
                self._logger.error(
                    "No permission to access julgar text channel",
                    extra={"context": {"channel_id": str(monitored_channel_id)}},
                )
                return
            except discord.HTTPException as exc:
                self._logger.error(
                    "Failed to resolve julgar text channel",
                    extra={
                        "context": {
                            "channel_id": str(monitored_channel_id),
                            "error": str(exc),
                        }
                    },
                )
                return

        if not isinstance(channel, discord.TextChannel):
            self._logger.warning(
                "Configured julgar channel is not a text channel",
                extra={
                    "context": {
                        "channel_id": str(monitored_channel_id),
                        "channel_type": str(channel.type),
                    }
                },
            )
            return

        self._logger.info(
            "Monitoring julgar text channel is active",
            extra={
                "context": {
                    "guild_id": str(channel.guild.id),
                    "guild_name": channel.guild.name,
                    "channel_id": str(channel.id),
                    "channel_name": channel.name,
                }
            },
        )
