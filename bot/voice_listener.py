import logging
from datetime import datetime, timezone
from typing import Any, Dict

import discord

from .webhook import WebhookDispatcher


class VoiceListener:
    def __init__(self, voice_channel_id: int, webhook: WebhookDispatcher) -> None:
        self._logger = logging.getLogger(__name__)
        self._voice_channel_id = voice_channel_id
        self._webhook = webhook

    @property
    def voice_channel_id(self) -> int:
        return self._voice_channel_id

    async def handle_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        old_channel_id = before.channel.id if before.channel else None
        new_channel_id = after.channel.id if after.channel else None

        entered_target_channel = (
            old_channel_id != self._voice_channel_id
            and new_channel_id == self._voice_channel_id
        )

        if not entered_target_channel:
            return

        guild = member.guild
        channel = after.channel
        if channel is None:
            # Defensive check: expected non-None due to entered_target_channel.
            return

        payload = self._build_payload(member=member, guild=guild, channel=channel)

        self._logger.info(
            "User joined monitored voice channel",
            extra={
                "context": {
                    "guild_id": str(guild.id),
                    "channel_id": str(channel.id),
                    "user_id": str(member.id),
                }
            },
        )

        await self._webhook.send_event(payload)

    def _build_payload(
        self,
        member: discord.Member,
        guild: discord.Guild,
        channel: discord.abc.GuildChannel,
    ) -> Dict[str, Any]:
        return {
            "event": "USER_JOINED_MONITORED_VOICE_CHANNEL",
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "guild": {
                "id": str(guild.id),
                "name": guild.name,
            },
            "channel": {
                "id": str(channel.id),
                "name": channel.name,
            },
            "user": {
                "id": str(member.id),
                "username": member.name,
                "tag": str(member),
            },
        }

