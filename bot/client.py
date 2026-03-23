import logging
from datetime import datetime, timezone
from typing import Optional, Set

import discord
from discord import app_commands

from .calendar_listener import CalendarListener
from .julgar_listener import JulgarListener
from .notion_client import NotionClient
from .task_views import CreateTaskModal, StopTimerSelectView
from .timer_manager import TimerManager
from .voice_listener import VoiceListener

STATUS_INDICATORS = {
    "Not started": "⬜",
    "In progress": "🔵",
    "Done": "✅",
}
DEFAULT_STATUS_INDICATOR = "⚪"


class VoiceWatcherClient(discord.Client):
    def __init__(
        self,
        voice_listener: VoiceListener,
        julgar_listener: JulgarListener,
        notion_client: Optional[NotionClient] = None,
        timer_manager: Optional[TimerManager] = None,
        calendar_listener: Optional[CalendarListener] = None,
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
        self._status_options_cache: Optional[list] = None
        self._dm_log_subscribers: Set[int] = set()
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

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

    def _register_commands(self) -> None:
        @self.tree.command(name="tasks", description="Lista suas tarefas do Notion")
        async def tasks_command(interaction: discord.Interaction) -> None:
            await self._handle_tasks(interaction)

        @self.tree.command(name="create-task", description="Cria uma nova tarefa no Notion")
        async def create_task_command(interaction: discord.Interaction) -> None:
            await self._handle_create_task(interaction)

        @self.tree.command(name="stop-timer", description="Para um cronômetro ativo e salva o tempo")
        async def stop_timer_command(interaction: discord.Interaction) -> None:
            await self._handle_stop_timer(interaction)

        @self.tree.command(name="logs", description="Ativa/desativa logs de comandos no seu DM")
        @app_commands.describe(enabled="Ativar (True) ou desativar (False) os logs no DM")
        async def logs_command(interaction: discord.Interaction, enabled: bool) -> None:
            await self._handle_logs_toggle(interaction, enabled)

    async def _handle_logs_toggle(self, interaction: discord.Interaction, enabled: bool) -> None:
        if enabled:
            self._dm_log_subscribers.add(interaction.user.id)
            await interaction.response.send_message(
                "Logs ativados. Você receberá notificações no DM sobre comandos recebidos.",
                ephemeral=True,
            )
        else:
            self._dm_log_subscribers.discard(interaction.user.id)
            await interaction.response.send_message(
                "Logs desativados.", ephemeral=True
            )

    async def _notify_dm_log(self, interaction: discord.Interaction) -> None:
        if not self._dm_log_subscribers:
            return

        cmd_name = interaction.command.qualified_name if interaction.command else "unknown"
        user = interaction.user
        channel_info = (
            f"DM" if interaction.guild is None
            else f"#{interaction.channel.name if interaction.channel else '?'} ({interaction.guild.name})"
        )
        now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

        log_msg = (
            f"```\n"
            f"[{now}] /{cmd_name}\n"
            f"User:    {user} ({user.id})\n"
            f"Channel: {channel_info}\n"
            f"```"
        )

        for uid in list(self._dm_log_subscribers):
            try:
                dm_user = self.get_user(uid) or await self.fetch_user(uid)
                dm_channel = dm_user.dm_channel or await dm_user.create_dm()
                await dm_channel.send(log_msg)
            except Exception as exc:
                self._logger.warning(
                    "Failed to send DM log",
                    extra={"context": {"user_id": uid, "error": str(exc)}},
                )

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type == discord.InteractionType.application_command:
            await self._notify_dm_log(interaction)

    async def _handle_tasks(self, interaction: discord.Interaction) -> None:
        if not self._notion_client:
            await interaction.response.send_message(
                "Notion não está configurado. Defina `NOTION_TOKEN` e `NOTION_DATABASE_ID` nas variáveis de ambiente.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            tasks = await self._notion_client.fetch_tasks()
        except Exception as exc:
            self._logger.error(
                "Failed to fetch Notion tasks",
                extra={"context": {"error": str(exc)}},
            )
            await interaction.followup.send(
                f"Erro ao buscar tarefas do Notion: `{exc}`",
                ephemeral=True,
            )
            return

        if not tasks:
            await interaction.followup.send(
                "Nenhuma tarefa encontrada no banco de dados.",
                ephemeral=True,
            )
            return

        embeds = self._build_task_embeds(tasks)
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i : i + 10], ephemeral=True)

    async def _handle_create_task(self, interaction: discord.Interaction) -> None:
        if not self._notion_client:
            await interaction.response.send_message(
                "Notion não está configurado. Defina `NOTION_TOKEN` e `NOTION_DATABASE_ID`.",
                ephemeral=True,
            )
            return

        status_options = await self._get_status_options()
        modal = CreateTaskModal(
            notion_client=self._notion_client,
            timer_manager=self._timer_manager,
            status_options=status_options,
        )
        await interaction.response.send_modal(modal)

    async def _handle_stop_timer(self, interaction: discord.Interaction) -> None:
        if not self._notion_client:
            await interaction.response.send_message(
                "Notion não está configurado.", ephemeral=True
            )
            return

        active = self._timer_manager.get_active(interaction.user.id)
        if not active:
            await interaction.response.send_message(
                "Você não tem nenhum cronômetro ativo.", ephemeral=True
            )
            return

        status_options = await self._get_status_options()
        view = StopTimerSelectView(
            notion_client=self._notion_client,
            timer_manager=self._timer_manager,
            status_options=status_options,
            user_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Você tem **{len(active)}** cronômetro(s) ativo(s). Qual deseja parar?",
            view=view,
            ephemeral=True,
        )

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

    async def setup_hook(self) -> None:
        synced = await self.tree.sync()
        self._logger.info(
            "Slash commands synced",
            extra={"context": {"commands": [c.name for c in synced]}},
        )

    async def on_ready(self) -> None:
        user_display = f"{self.user} ({self.user.id})" if self.user else "unknown"
        self._logger.info(
            "Discord bot connected to gateway",
            extra={"context": {"bot_user": user_display}},
        )
        await self._log_monitored_channel_status()
        await self._log_julgar_channel_status()

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
        await self._julgar_listener.handle_message(self, message)
        if self._calendar_listener is not None:
            await self._calendar_listener.handle_message(self, message)

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
