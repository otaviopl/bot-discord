"""Dublês do Discord usados pelos testes do listener."""

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional


class FakeUser:
    def __init__(self, user_id: int, name: str = "Alguem") -> None:
        self.id = user_id
        self.display_name = name
        self.bot = False
        self.mention = f"<@{user_id}>"
        self.dms: List[Any] = []

    async def send(self, content: str = None, embed: Any = None, **kwargs) -> "FakeMessage":
        self.dms.append(embed if embed is not None else content)
        return FakeMessage("", self, FakeChannel(999))


class FakeMessageRef:
    """Mensagem ja publicada no canal (painel/resumo)."""

    _next_id = 1000

    def __init__(self, embed: Any, channel: "FakeChannel") -> None:
        FakeMessageRef._next_id += 1
        self.id = FakeMessageRef._next_id
        self.embed = embed
        self.channel = channel
        self.edits = 0
        self.pinned = False

    async def edit(self, embed: Any = None, **kwargs) -> None:
        import discord

        if self.id not in self.channel.messages:
            raise discord.NotFound(_FakeResponse(), "mensagem apagada")
        self.embed = embed
        self.edits += 1

    async def pin(self) -> None:
        self.pinned = True


class FakeChannel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.sent: List[Any] = []
        self.messages: Dict[int, FakeMessageRef] = {}

    async def send(self, content: str = None, embed: Any = None, **kwargs) -> FakeMessageRef:
        payload = embed if embed is not None else content
        self.sent.append(payload)
        ref = FakeMessageRef(payload, self)
        self.messages[ref.id] = ref
        return ref

    async def fetch_message(self, message_id: int) -> FakeMessageRef:
        import discord

        if message_id not in self.messages:
            raise discord.NotFound(_FakeResponse(), "mensagem apagada")
        return self.messages[message_id]

    @asynccontextmanager
    async def typing(self):
        yield


class _FakeResponse:
    status = 404
    reason = "Not Found"


class FakeGuild:
    def __init__(self, guild_id: int, members: Dict[int, FakeUser]) -> None:
        self.id = guild_id
        self._members = members

    def get_member(self, user_id: int) -> Optional[FakeUser]:
        return self._members.get(user_id)


class FakeAttachment:
    """Anexo do Discord: o bot só usa filename, size e read()."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self._data = data
        self.size = len(data)

    async def read(self) -> bytes:
        return self._data


class FakeMessage:
    def __init__(
        self,
        content: str,
        author: FakeUser,
        channel: FakeChannel,
        guild=None,
        attachments=None,
    ) -> None:
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.attachments = attachments or []


class FakeDiscordClient:
    def __init__(self, guild: FakeGuild, channels: Dict[int, FakeChannel]) -> None:
        self._guild = guild
        self._channels = channels
        self.users: Dict[int, FakeUser] = {}

    def get_guild(self, guild_id: int) -> Optional[FakeGuild]:
        return self._guild if self._guild.id == guild_id else None

    def get_user(self, user_id: int) -> Optional[FakeUser]:
        return self.users.get(user_id)

    async def fetch_user(self, user_id: int) -> FakeUser:
        return self.users.setdefault(user_id, FakeUser(user_id))

    def get_channel(self, channel_id: int) -> Optional[FakeChannel]:
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        return self._channels[channel_id]
