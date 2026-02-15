import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Set, Tuple

import discord

ActionType = Literal["kick", "castigo", "mute", "disconnect_adm"]


class JulgarListener:
    def __init__(self, text_channel_id: int, adm_voice_channel_id: int) -> None:
        self._logger = logging.getLogger(__name__)
        self._text_channel_id = text_channel_id
        self._adm_voice_channel_id = adm_voice_channel_id
        self._active_sessions: Set[Tuple[int, int]] = set()

    @property
    def text_channel_id(self) -> int:
        return self._text_channel_id

    async def handle_message(self, client: discord.Client, message: discord.Message) -> None:
        if message.author.bot:
            return

        if message.channel.id != self._text_channel_id:
            return

        content = message.content.strip().lower()
        if content == "!help":
            await message.channel.send(self._build_help_text())
            return

        if content == "!julgar-regras":
            await message.channel.send(self._build_rules_text())
            return

        if content != "!julgar":
            return

        guild = message.guild
        if guild is None:
            return

        session_key = (message.channel.id, message.author.id)
        if session_key in self._active_sessions:
            await message.channel.send(
                f"{message.author.mention} voce ja possui uma selecao em andamento. "
                "Responda com um numero de 1 a 5."
            )
            return

        self._active_sessions.add(session_key)
        try:
            candidates = await self._fetch_first_five_users(guild=guild)
            if not candidates:
                await message.channel.send(
                    f"{message.author.mention} nao encontrei usuarios para julgamento neste servidor."
                )
                return

            prompt_text = self._build_prompt_text(message=message, candidates=candidates)
            await message.channel.send(prompt_text)

            selected_member = await self._wait_for_user_choice(
                client=client,
                trigger_message=message,
                candidates=candidates,
            )
            if selected_member is None:
                await message.channel.send(
                    f"{message.author.mention} tempo esgotado. Envie `!julgar` novamente para tentar."
                )
                return

            await message.channel.send(
                f"{message.author.mention} voce escolheu **{selected_member}** para julgamento."
            )

            await message.channel.send(
                f"{message.author.mention} escolha a acao para **{selected_member}**:\n"
                "1. kick\n"
                "2. castigo (10 minutos)\n"
                "3. mute (10 minutos)\n"
                "4. disconnect do canal ADM"
            )
            selected_action = await self._wait_for_action_choice(
                client=client,
                trigger_message=message,
            )
            if selected_action is None:
                await message.channel.send(
                    f"{message.author.mention} tempo esgotado para escolher a acao. "
                    "Envie `!julgar` novamente para tentar."
                )
                return

            await message.channel.send(
                f"{message.author.mention} agora escolha seu numero da sorte (1-10)."
            )
            chosen_number = await self._wait_for_lucky_number(
                client=client,
                trigger_message=message,
            )
            if chosen_number is None:
                await message.channel.send(
                    f"{message.author.mention} tempo esgotado para escolher o numero. "
                    "Envie `!julgar` novamente para tentar."
                )
                return

            rolled_number = random.randint(1, 10)
            actor_member = message.author if isinstance(message.author, discord.Member) else guild.get_member(message.author.id)
            if actor_member is None:
                actor_member = await guild.fetch_member(message.author.id)

            action_target = selected_member if chosen_number == rolled_number else actor_member
            luck_message = (
                f"Numero escolhido: **{chosen_number}** | numero sorteado: **{rolled_number}**.\n"
                f"{'Sorte! Seu desejo foi realizado.' if chosen_number == rolled_number else 'Sem sorte! A acao voltou para voce.'}"
            )
            await message.channel.send(luck_message)

            success, action_result = await self._apply_action(
                guild=guild,
                actor=actor_member,
                target=action_target,
                action=selected_action,
            )
            await message.channel.send(action_result)
            if not success:
                return
        finally:
            self._active_sessions.discard(session_key)

        self._logger.info(
            "Julgar command processed in monitored text channel",
            extra={
                "context": {
                    "guild_id": str(guild.id),
                    "channel_id": str(message.channel.id),
                    "user_id": str(message.author.id),
                    "message_id": str(message.id),
                }
            },
        )

    async def _fetch_first_five_users(self, guild: discord.Guild) -> List[discord.Member]:
        candidates: List[discord.Member] = []
        async for member in guild.fetch_members(limit=100):
            if member.bot:
                continue
            candidates.append(member)
            if len(candidates) == 5:
                break
        return candidates

    async def _wait_for_user_choice(
        self,
        client: discord.Client,
        trigger_message: discord.Message,
        candidates: List[discord.Member],
    ) -> discord.Member | None:
        def check(incoming: discord.Message) -> bool:
            return (
                incoming.channel.id == trigger_message.channel.id
                and incoming.author.id == trigger_message.author.id
            )

        while True:
            try:
                response = await client.wait_for("message", check=check, timeout=60.0)
            except asyncio.TimeoutError:
                return None

            content = response.content.strip()
            if content.isdigit():
                selected_index = int(content)
                if 1 <= selected_index <= len(candidates):
                    return candidates[selected_index - 1]

            await trigger_message.channel.send(
                f"{trigger_message.author.mention} resposta invalida. "
                f"Escolha um numero entre 1 e {len(candidates)}."
            )

    async def _wait_for_action_choice(
        self,
        client: discord.Client,
        trigger_message: discord.Message,
    ) -> ActionType | None:
        def check(incoming: discord.Message) -> bool:
            return (
                incoming.channel.id == trigger_message.channel.id
                and incoming.author.id == trigger_message.author.id
            )

        mapping = {
            "1": "kick",
            "kick": "kick",
            "2": "castigo",
            "castigo": "castigo",
            "3": "mute",
            "mute": "mute",
            "4": "disconnect",
            "disconnect": "disconnect_adm",
            "disconnect_adm": "disconnect_adm",
        }

        while True:
            try:
                response = await client.wait_for("message", check=check, timeout=60.0)
            except asyncio.TimeoutError:
                return None

            selected = mapping.get(response.content.strip().lower())
            if selected is not None:
                return selected

            await trigger_message.channel.send(
                f"{trigger_message.author.mention} resposta invalida. "
                "Escolha 1-4 (kick/castigo/mute/disconnect)."
            )

    async def _wait_for_lucky_number(
        self,
        client: discord.Client,
        trigger_message: discord.Message,
    ) -> int | None:
        def check(incoming: discord.Message) -> bool:
            return (
                incoming.channel.id == trigger_message.channel.id
                and incoming.author.id == trigger_message.author.id
            )

        while True:
            try:
                response = await client.wait_for("message", check=check, timeout=60.0)
            except asyncio.TimeoutError:
                return None

            content = response.content.strip()
            if content.isdigit():
                selected_number = int(content)
                if 1 <= selected_number <= 10:
                    return selected_number

            await trigger_message.channel.send(
                f"{trigger_message.author.mention} resposta invalida. Escolha um numero entre 1 e 10."
            )

    async def _apply_action(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        target: discord.Member,
        action: ActionType,
    ) -> tuple[bool, str]:
        reason = f"Acao !julgar solicitada por {actor} ({actor.id})"

        try:
            if action == "kick":
                await guild.kick(target, reason=reason)
                return True, f"Acao aplicada: **kick** em {target.mention}."

            if action == "castigo":
                until = datetime.now(timezone.utc) + timedelta(minutes=10)
                await target.edit(timed_out_until=until, reason=reason)
                return True, f"Acao aplicada: **castigo** em {target.mention} por 10 minutos."

            if action == "mute":
                if target.voice is None or target.voice.channel is None:
                    return False, f"Nao foi possivel aplicar mute: {target.mention} nao esta em canal de voz."
                await target.edit(mute=True, reason=reason)
                asyncio.create_task(self._remove_mute_after_delay(guild, target.id, 600))
                return True, f"Acao aplicada: **mute** em {target.mention} por 10 minutos."

            if target.voice is None or target.voice.channel is None:
                return False, f"Nao foi possivel desconectar: {target.mention} nao esta em canal de voz."
            if target.voice.channel.id != self._adm_voice_channel_id:
                return (
                    False,
                    f"Nao foi possivel desconectar: {target.mention} nao esta no canal ADM configurado.",
                )
            await target.move_to(None, reason=reason)
            return True, f"Acao aplicada: **disconnect** de {target.mention} do canal ADM."
        except discord.Forbidden:
            return False, "Nao tenho permissao suficiente para aplicar esta acao."
        except discord.HTTPException as exc:
            self._logger.warning(
                "Failed to apply julgar action",
                extra={
                    "context": {
                        "guild_id": str(guild.id),
                        "actor_id": str(actor.id),
                        "target_id": str(target.id),
                        "action": action,
                        "error": str(exc),
                    }
                },
            )
            return False, "A acao falhou por erro da API do Discord."

    async def _remove_mute_after_delay(self, guild: discord.Guild, member_id: int, delay_seconds: int) -> None:
        await asyncio.sleep(delay_seconds)

        member = guild.get_member(member_id)
        if member is None:
            return
        if member.voice is None or member.voice.channel is None:
            return

        try:
            await member.edit(mute=False, reason="Fim do mute temporario do !julgar")
        except discord.HTTPException:
            self._logger.warning(
                "Failed to remove temporary mute from user",
                extra={"context": {"guild_id": str(guild.id), "member_id": str(member_id)}},
            )

    def _build_prompt_text(
        self,
        message: discord.Message,
        candidates: List[discord.Member],
    ) -> str:
        lines = [f"{message.author.mention} escolha quem deve ser julgado (1-{len(candidates)}):"]
        for index, member in enumerate(candidates, start=1):
            lines.append(f"{index}. {member} (id: {member.id})")
        return "\n".join(lines)

    def _build_help_text(self) -> str:
        return (
            "Comandos disponiveis:\n"
            "- `!julgar`: inicia o julgamento com lista de usuarios.\n"
            "- `!julgar-regras`: mostra as regras do jogo."
        )

    def _build_rules_text(self) -> str:
        return (
            "Regras do !julgar:\n"
            "1. Use `!julgar` para escolher um usuario da lista.\n"
            "2. Escolha a acao: kick/castigo(10min)/mute(10min)/disconnect do canal ADM.\n"
            "3. Escolha um numero de 1 a 10.\n"
            "4. O bot sorteia outro numero de 1 a 10.\n"
            "5. Se os numeros baterem, a acao vai no usuario escolhido.\n"
            "6. Se nao baterem, a acao volta para voce."
        )

