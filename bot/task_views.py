import logging
from datetime import datetime, timezone
from typing import List, Optional

import discord

from .notion_client import NotionClient
from .timer_manager import TimerManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# !create-task flow
# ---------------------------------------------------------------------------

class StatusSelectView(discord.ui.View):
    def __init__(
        self,
        notion_client: NotionClient,
        timer_manager: TimerManager,
        task_name: str,
        task_description: Optional[str],
        task_categories: Optional[List[str]],
        status_options: List[str],
    ) -> None:
        super().__init__(timeout=120)
        self._notion = notion_client
        self._timer = timer_manager
        self._task_name = task_name
        self._task_description = task_description
        self._task_categories = task_categories or []

        select = discord.ui.Select(
            placeholder="Selecione o status...",
            options=[
                discord.SelectOption(label=s, value=s) for s in status_options[:25]
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        status = interaction.data["values"][0]  # type: ignore[index]
        await interaction.response.defer()

        try:
            task = await self._notion.create_task(
                name=self._task_name,
                status=status,
                description=self._task_description,
                categories=self._task_categories,
            )
        except Exception as exc:
            logger.error("Failed to create Notion task", extra={"context": {"error": str(exc)}})
            embed = discord.Embed(
                title="❌ Erro ao criar tarefa",
                description=f"```{exc}```",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(
            title="✅ Tarefa criada",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Nome", value=f"[{task['name']}]({task['url']})", inline=False)
        embed.add_field(name="Status", value=f"`{status}`", inline=True)
        if self._task_categories:
            embed.add_field(
                name="Categorias",
                value=", ".join(f"`{category}`" for category in self._task_categories),
                inline=False,
            )
        if self._task_description:
            embed.add_field(name="Descrição", value=self._task_description[:200], inline=False)
        embed.set_footer(text="Notion")

        view = StartTimerView(
            timer_manager=self._timer,
            task_id=task["id"],
            task_name=task["name"],
            task_url=task["url"],
        )
        await interaction.followup.send(
            content="Deseja iniciar o cronômetro?",
            embed=embed,
            view=view,
        )
        self.stop()


class StartTimerView(discord.ui.View):
    def __init__(
        self,
        timer_manager: TimerManager,
        task_id: str,
        task_name: str,
        task_url: str,
    ) -> None:
        super().__init__(timeout=60)
        self._timer = timer_manager
        self._task_id = task_id
        self._task_name = task_name
        self._task_url = task_url

    @discord.ui.button(label="Sim, iniciar timer", style=discord.ButtonStyle.success, emoji="⏱️")
    async def start_yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = self._timer.start(
            user_id=interaction.user.id,
            task_id=self._task_id,
            task_name=self._task_name,
            task_url=self._task_url,
        )
        embed = discord.Embed(
            title="⏱️ Cronômetro iniciado",
            description=f"Tarefa: **{entry.task_name}**\nUse `!stop-timer` para parar.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed)
        self.stop()

    @discord.ui.button(label="Não", style=discord.ButtonStyle.secondary)
    async def start_no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = discord.Embed(
            description="Ok, tarefa criada sem cronômetro.",
            color=discord.Color.light_grey(),
        )
        await interaction.response.send_message(embed=embed)
        self.stop()


# ---------------------------------------------------------------------------
# !start-timer flow (existing tasks)
# ---------------------------------------------------------------------------

class StartTimerFromListView(discord.ui.View):
    def __init__(
        self,
        timer_manager: TimerManager,
        tasks_list: list,
    ) -> None:
        super().__init__(timeout=120)
        self._timer = timer_manager
        self._tasks_by_id = {t["id"]: t for t in tasks_list}

        select = discord.ui.Select(
            placeholder="Selecione a tarefa...",
            options=[
                discord.SelectOption(
                    label=t["name"][:100],
                    value=t["id"],
                    description=(t.get("property_status") or "Sem status")[:100],
                )
                for t in tasks_list[:25]
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        task_id = interaction.data["values"][0]  # type: ignore[index]
        task = self._tasks_by_id.get(task_id)
        if not task:
            await interaction.response.send_message(
                embed=discord.Embed(title="❌ Tarefa não encontrada", color=discord.Color.red()),
            )
            self.stop()
            return

        entry = self._timer.start(
            user_id=interaction.user.id,
            task_id=task["id"],
            task_name=task["name"],
            task_url=task.get("url", ""),
        )

        embed = discord.Embed(
            title="⏱️ Cronômetro iniciado",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Tarefa", value=f"**[{entry.task_name}]({entry.task_url})**", inline=False)
        status = task.get("property_status") or "N/A"
        embed.add_field(name="Status atual", value=f"`{status}`", inline=True)
        embed.set_footer(text="Use !stop-timer para parar")

        await interaction.response.send_message(embed=embed)
        self.stop()


# ---------------------------------------------------------------------------
# !stop-timer flow
# ---------------------------------------------------------------------------

class StopTimerSelectView(discord.ui.View):
    def __init__(
        self,
        notion_client: NotionClient,
        timer_manager: TimerManager,
        status_options: List[str],
        user_id: int,
    ) -> None:
        super().__init__(timeout=120)
        self._notion = notion_client
        self._timer = timer_manager
        self._status_options = status_options
        self._user_id = user_id

        active = self._timer.get_active(user_id)
        select = discord.ui.Select(
            placeholder="Selecione o timer para parar...",
            options=[
                discord.SelectOption(
                    label=t.task_name,
                    value=t.task_id,
                    description=f"⏱️ Rodando há {t.elapsed_display}",
                )
                for t in active[:25]
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        task_id = interaction.data["values"][0]  # type: ignore[index]
        entry = self._timer.stop(self._user_id, task_id)
        if not entry:
            embed = discord.Embed(
                title="❌ Timer não encontrado",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=embed)
            self.stop()
            return

        elapsed = entry.elapsed_minutes

        embed = discord.Embed(
            title="⏹️ Timer parado",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Tarefa", value=f"**{entry.task_name}**", inline=False)
        embed.add_field(name="Tempo da sessão", value=f"`{entry.elapsed_display}` ({elapsed} min)", inline=True)
        embed.set_footer(text="Escolha o novo status abaixo")

        view = StopTimerStatusView(
            notion_client=self._notion,
            status_options=self._status_options,
            task_id=entry.task_id,
            task_name=entry.task_name,
            task_url=entry.task_url,
            elapsed_minutes=elapsed,
        )
        await interaction.response.send_message(embed=embed, view=view)
        self.stop()


class StopTimerStatusView(discord.ui.View):
    def __init__(
        self,
        notion_client: NotionClient,
        status_options: List[str],
        task_id: str,
        task_name: str,
        task_url: str,
        elapsed_minutes: int,
    ) -> None:
        super().__init__(timeout=120)
        self._notion = notion_client
        self._task_id = task_id
        self._task_name = task_name
        self._task_url = task_url
        self._elapsed_minutes = elapsed_minutes

        select = discord.ui.Select(
            placeholder="Selecione o novo status...",
            options=[
                discord.SelectOption(label=s, value=s) for s in status_options[:25]
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        new_status = interaction.data["values"][0]  # type: ignore[index]
        await interaction.response.defer()

        try:
            total_time = await self._notion.update_task(
                page_id=self._task_id,
                time_min=self._elapsed_minutes,
                status=new_status,
            )
        except Exception as exc:
            logger.error("Failed to update Notion task", extra={"context": {"error": str(exc)}})
            embed = discord.Embed(
                title="❌ Erro ao atualizar tarefa",
                description=f"```{exc}```",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        embed = discord.Embed(
            title="✅ Tarefa atualizada",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Tarefa", value=f"[{self._task_name}]({self._task_url})", inline=False)
        embed.add_field(name="Sessão", value=f"`{self._elapsed_minutes} min`", inline=True)
        if total_time > self._elapsed_minutes:
            embed.add_field(name="Total acumulado", value=f"`{total_time} min`", inline=True)
        embed.add_field(name="Status", value=f"`{new_status}`", inline=True)
        embed.set_footer(text="Notion")

        await interaction.followup.send(embed=embed)
        self.stop()
