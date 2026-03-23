import logging
from typing import Optional

import discord
from discord import app_commands

from .julgar_listener import JulgarListener
from .notion_client import NotionClient
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
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.messages = True
        intents.message_content = True
        intents.members = True

        super().__init__(intents=intents)
        self._logger = logging.getLogger(__name__)
        self._voice_listener = voice_listener
        self._julgar_listener = julgar_listener
        self._notion_client = notion_client
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="tasks", description="Lista suas tarefas do Notion")
        async def tasks_command(interaction: discord.Interaction) -> None:
            await self._handle_tasks(interaction)

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

