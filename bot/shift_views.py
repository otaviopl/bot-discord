import logging
from datetime import datetime, timezone

import discord

from .notion_client import NotionClient
from .shift_manager import (
    parse_entries,
    serialize_entries,
    calculate_summary,
    format_duration,
    build_history_line,
    is_shift_open,
    parse_shift_page,
)

logger = logging.getLogger(__name__)


class ShiftEditView(discord.ui.View):
    def __init__(
        self,
        notion_client: NotionClient,
        shift: dict,
    ) -> None:
        super().__init__(timeout=120)
        self._notion = notion_client
        self._shift = shift

    @discord.ui.button(label="Desfazer última entrada", style=discord.ButtonStyle.primary, emoji="↩️")
    async def undo_last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entries = list(self._shift["entries"])
        if not entries:
            await interaction.response.send_message(
                embed=discord.Embed(description="Nenhuma entrada para desfazer.", color=discord.Color.orange()),
            )
            self.stop()
            return

        removed = entries.pop()
        new_json = serialize_entries(entries)

        try:
            await self._notion.update_shift_entries(self._shift["id"], new_json)
        except Exception as exc:
            logger.error("Failed to undo shift entry", extra={"context": {"error": str(exc)}})
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Erro", description=f"```{exc}```", color=discord.Color.red()),
            )
            self.stop()
            return

        self._shift["entries"] = entries

        embed = discord.Embed(
            title="↩️ Entrada removida",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Removida", value=f"`{removed}`", inline=True)
        embed.add_field(name="Histórico", value=build_history_line(entries) or "-", inline=False)
        await interaction.response.send_message(embed=embed)
        self.stop()

    @discord.ui.button(label="Entrada manual", style=discord.ButtonStyle.primary, emoji="✏️")
    async def manual_entry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = discord.Embed(
            description="Digite o horário no formato **HH:MM** (ex: `14:30`)",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

        def check(m: discord.Message) -> bool:
            return m.author.id == interaction.user.id and m.channel.id == interaction.channel_id

        try:
            msg = await interaction.client.wait_for("message", check=check, timeout=60)
        except Exception:
            await interaction.followup.send(
                embed=discord.Embed(description="⏰ Tempo esgotado.", color=discord.Color.orange()),
            )
            self.stop()
            return

        time_str = msg.content.strip()
        try:
            datetime.strptime(time_str, "%H:%M")
        except ValueError:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Formato inválido",
                    description=f"`{time_str}` não é um horário válido. Use **HH:MM**.",
                    color=discord.Color.red(),
                ),
            )
            self.stop()
            return

        entries = list(self._shift["entries"])
        entries.append(time_str)
        new_json = serialize_entries(entries)

        try:
            await self._notion.update_shift_entries(self._shift["id"], new_json)
        except Exception as exc:
            logger.error("Failed to add manual entry", extra={"context": {"error": str(exc)}})
            await interaction.followup.send(
                embed=discord.Embed(title="❌ Erro", description=f"```{exc}```", color=discord.Color.red()),
            )
            self.stop()
            return

        self._shift["entries"] = entries
        status = "Trabalhando" if is_shift_open(entries) else "Pausa / Encerrado"

        embed = discord.Embed(
            title="✏️ Entrada manual adicionada",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Horário", value=f"`{time_str}`", inline=True)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Histórico", value=build_history_line(entries), inline=False)
        await interaction.followup.send(embed=embed)
        self.stop()

    @discord.ui.button(label="Deletar turno", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_shift(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            await self._notion.delete_shift(self._shift["id"])
        except Exception as exc:
            logger.error("Failed to delete shift", extra={"context": {"error": str(exc)}})
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Erro", description=f"```{exc}```", color=discord.Color.red()),
            )
            self.stop()
            return

        embed = discord.Embed(
            title="🗑️ Turno deletado",
            description=f"Turno **{self._shift['name']}** foi removido.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed)
        self.stop()
