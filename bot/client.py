import logging

import discord

from .voice_listener import VoiceListener


class VoiceWatcherClient(discord.Client):
    def __init__(self, voice_listener: VoiceListener) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True

        super().__init__(intents=intents)
        self._logger = logging.getLogger(__name__)
        self._voice_listener = voice_listener

    async def on_ready(self) -> None:
        user_display = f"{self.user} ({self.user.id})" if self.user else "unknown"
        self._logger.info(
            "Discord bot connected to gateway",
            extra={"context": {"bot_user": user_display}},
        )
        await self._log_monitored_channel_status()

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

