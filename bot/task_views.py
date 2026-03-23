import logging
from typing import List

import discord

from .notion_client import NotionClient
from .timer_manager import TimerManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /create-task flow
# ---------------------------------------------------------------------------

class CreateTaskModal(discord.ui.Modal, title="Nova Tarefa"):
    task_name = discord.ui.TextInput(
        label="Nome da tarefa",
        placeholder="Ex: Implementar API de pagamentos",
        required=True,
        max_length=200,
    )
    task_description = discord.ui.TextInput(
        label="Descrição (opcional)",
        style=discord.TextStyle.paragraph,
        placeholder="Detalhes sobre a tarefa...",
        required=False,
        max_length=2000,
    )

    def __init__(
        self,
        notion_client: NotionClient,
        timer_manager: TimerManager,
        status_options: List[str],
    ) -> None:
        super().__init__()
        self._notion = notion_client
        self._timer = timer_manager
        self._status_options = status_options

    async def on_submit(self, interaction: discord.Interaction) -> None:
        view = StatusSelectView(
            notion_client=self._notion,
            timer_manager=self._timer,
            task_name=self.task_name.value,
            task_description=self.task_description.value or None,
            status_options=self._status_options,
        )
        await interaction.response.send_message(
            f"**Tarefa:** {self.task_name.value}\nEscolha o status inicial:",
            view=view,
            ephemeral=True,
        )


class StatusSelectView(discord.ui.View):
    def __init__(
        self,
        notion_client: NotionClient,
        timer_manager: TimerManager,
        task_name: str,
        task_description: str | None,
        status_options: List[str],
    ) -> None:
        super().__init__(timeout=120)
        self._notion = notion_client
        self._timer = timer_manager
        self._task_name = task_name
        self._task_description = task_description

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
        await interaction.response.defer(ephemeral=True)

        try:
            task = await self._notion.create_task(
                name=self._task_name,
                status=status,
                description=self._task_description,
            )
        except Exception as exc:
            logger.error("Failed to create Notion task", extra={"context": {"error": str(exc)}})
            await interaction.followup.send(
                f"Erro ao criar tarefa no Notion: `{exc}`", ephemeral=True
            )
            return

        view = StartTimerView(
            timer_manager=self._timer,
            task_id=task["id"],
            task_name=task["name"],
            task_url=task["url"],
        )
        await interaction.followup.send(
            f"✅ Tarefa **[{task['name']}]({task['url']})** criada com status `{status}`!\n\nDeseja iniciar o cronômetro?",
            view=view,
            ephemeral=True,
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

    @discord.ui.button(label="Sim, iniciar timer", style=discord.ButtonStyle.success)
    async def start_yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        entry = self._timer.start(
            user_id=interaction.user.id,
            task_id=self._task_id,
            task_name=self._task_name,
            task_url=self._task_url,
        )
        await interaction.response.send_message(
            f"⏱️ Cronômetro iniciado para **{entry.task_name}**!",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Não", style=discord.ButtonStyle.secondary)
    async def start_no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message("Ok, tarefa criada sem cronômetro.", ephemeral=True)
        self.stop()


# ---------------------------------------------------------------------------
# /stop-timer flow
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
                    label=f"{t.task_name} ({t.elapsed_display})",
                    value=t.task_id,
                    description=f"Rodando há {t.elapsed_display}",
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
            await interaction.response.send_message("Timer não encontrado.", ephemeral=True)
            self.stop()
            return

        elapsed = entry.elapsed_minutes

        view = StopTimerStatusView(
            notion_client=self._notion,
            status_options=self._status_options,
            task_id=entry.task_id,
            task_name=entry.task_name,
            task_url=entry.task_url,
            elapsed_minutes=elapsed,
        )
        await interaction.response.send_message(
            f"⏹️ Timer parado para **{entry.task_name}**\n"
            f"Tempo registrado: **{entry.elapsed_display}** ({elapsed} min)\n\n"
            f"Qual status deseja atribuir à tarefa?",
            view=view,
            ephemeral=True,
        )
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
        await interaction.response.defer(ephemeral=True)

        try:
            await self._notion.update_task(
                page_id=self._task_id,
                time_min=self._elapsed_minutes,
                status=new_status,
            )
        except Exception as exc:
            logger.error("Failed to update Notion task", extra={"context": {"error": str(exc)}})
            await interaction.followup.send(
                f"Erro ao atualizar tarefa no Notion: `{exc}`", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"✅ **{self._task_name}** atualizada!\n"
            f"⏱️ Tempo: **{self._elapsed_minutes} min**\n"
            f"📋 Status: **{new_status}**\n"
            f"🔗 [Abrir no Notion]({self._task_url})",
            ephemeral=True,
        )
        self.stop()
