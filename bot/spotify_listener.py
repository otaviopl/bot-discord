"""Comandos, painel fixado, coleta de escutas e resumo semanal do Spotify."""

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord

from .spotify_auth import SCOPE_BIBLIOTECA, SpotifyAuth
from .spotify_client import (
    SpotifyAuthError,
    SpotifyClient,
    SpotifyForbidden,
    SpotifyRateLimited,
    SpotifyUnavailable,
)
from .spotify_import import ImportError_, parse_upload, resumo as resumo_import
from .spotify_listening import describe_sources, format_duration, resolve_listening
from .spotify_format import (
    SPOTIFY_GREEN,
    build_compare_embed,
    build_panel_embed,
    build_top_embed,
    fit_field,
    describe_playback,
    format_day_range,
    iso_week_key,
    month_bounds,
    parse_range,
    today_bounds,
    last_7_days,
    panel_fingerprint,
    parse_period,
    parse_week_offset,
    to_ms,
    week_bounds,
)
from .spotify_store import Play, SpotifyAccount, SpotifyStore

PANEL_MESSAGE_KEY = "panel_message_id"
WEEKLY_SUMMARY_PREFIX = "weekly_summary:"
MAX_RECENT_PAGES = 10

COMMANDS = (
    "!conectar", "!agora", "!top", "!comparar", "!desconectar",
    "!spotify", "!minutos", "!importar", "!curtidas", "!generos", "!gêneros",
)

MAX_IMPORT_BYTES = 25 * 1024 * 1024  # teto de anexo do Discord sem Nitro


def _embed_info(description: str) -> discord.Embed:
    return discord.Embed(description=description, color=SPOTIFY_GREEN)


def _embed_error(title: str, description: str = "") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _embed_forbidden(nome: str, exc: SpotifyForbidden) -> discord.Embed:
    """403 nao e expiracao: reconectar nao resolve, entao a mensagem tem que dizer
    o que de fato precisa ser feito."""
    if exc.conta_nao_cadastrada:
        return _embed_error(
            "🚫 Conta não liberada no app do Spotify",
            f"A conta de **{nome}** autorizou normalmente, mas o app está em "
            "Development Mode e só aceita contas cadastradas.\n\n"
            "**Como resolver:** no [dashboard do Spotify]"
            "(https://developer.spotify.com/dashboard) → o app → **Settings** → "
            "**User Management** → adicione o nome e o e-mail da conta dela.\n"
            "Depois é só rodar `!conectar` mais uma vez.\n\n"
            "_Reconectar antes disso não resolve: o problema é o cadastro, não a autorização._",
        )
    return _embed_error(
        "🚫 Spotify recusou a consulta",
        f"A conta de **{nome}** está conectada, mas o Spotify negou o acesso:\n"
        f"```{exc.motivo}```",
    )


class SpotifyListener:
    def __init__(
        self,
        store: SpotifyStore,
        auth: SpotifyAuth,
        client: SpotifyClient,
        guild_id: int,
        channel_id: int,
        allowed_user_ids: Tuple[int, ...],
        oauth_host: str,
        oauth_port: int,
        tz_name: str = "America/Sao_Paulo",
        http: Optional[Any] = None,
    ) -> None:
        self._store = store
        self._auth = auth
        self._api = client
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._allowed = tuple(allowed_user_ids)
        self._oauth_host = oauth_host
        self._oauth_port = oauth_port
        self._tz = ZoneInfo(tz_name)
        self._http = http
        self._logger = logging.getLogger(__name__)

        self._discord: Optional[discord.Client] = None
        self._panel_fingerprint: Optional[str] = None
        self._panel_message: Optional[discord.Message] = None
        self._last_playback: Dict[int, Tuple[Dict[str, Any], datetime]] = {}
        self._progress: Dict[int, Dict[str, Any]] = {}
        self._rate_limited_until: float = 0.0

    # ------------------------------------------------------------------ #
    # Ciclo de vida
    # ------------------------------------------------------------------ #

    async def start(self, client: discord.Client) -> None:
        self._discord = client
        await self._auth.start_callback_server(
            self._oauth_host, self._oauth_port, self._on_account_connected
        )

    async def close(self) -> None:
        await self._auth.stop_callback_server()
        if self._http is not None:
            await self._http.aclose()

    async def _on_account_connected(self, discord_user_id: int, display_name: Optional[str]) -> None:
        """Chamado pelo servidor de callback assim que uma conta e autorizada."""
        if self._discord is None:
            return
        try:
            user = await self._discord.fetch_user(discord_user_id)
            await user.send(
                embed=_embed_info(
                    f"✅ Spotify conectado{f' como **{display_name}**' if display_name else ''}.\n"
                    "A contagem de escutas começa agora — o bot não tem acesso ao que você ouviu antes disso."
                )
            )
        except Exception as exc:
            self._logger.warning(
                "Nao foi possivel avisar o usuario no DM",
                extra={"context": {"error": str(exc), "discord_user_id": str(discord_user_id)}},
            )
        # Painel entra em cena assim que a primeira conta conecta.
        self._panel_fingerprint = None

    # ------------------------------------------------------------------ #
    # Permissao
    # ------------------------------------------------------------------ #

    def _is_allowed(self, user_id: int) -> bool:
        return user_id in self._allowed

    def _channel_ok(self, message: discord.Message) -> bool:
        if isinstance(message.channel, discord.DMChannel):
            return True
        if message.guild is None or message.guild.id != self._guild_id:
            return False
        return message.channel.id == self._channel_id

    # ------------------------------------------------------------------ #
    # Roteador de comandos
    # ------------------------------------------------------------------ #

    async def handle_message(self, client: discord.Client, message: discord.Message) -> None:
        if message.author.bot:
            return

        content = message.content.strip()
        head = content.split(" ", 1)[0].lower()
        if head not in COMMANDS:
            return

        if not self._channel_ok(message):
            return

        if not self._is_allowed(message.author.id):
            self._logger.warning(
                "Usuario nao autorizado tentou comando do Spotify",
                extra={"context": {"user_id": str(message.author.id), "command": head}},
            )
            await message.channel.send(
                embed=_embed_error(
                    "🔒 Bot privado",
                    "Estes comandos estão restritos às duas contas configuradas.",
                )
            )
            return

        args = content.split(" ")[1:]
        self._logger.info(
            "Comando do Spotify recebido",
            extra={"context": {"command": head, "user_id": str(message.author.id), "args": args}},
        )

        try:
            if head == "!spotify":
                await message.channel.send(embed=self._help_embed())
            elif head == "!conectar":
                await self._cmd_conectar(message)
            elif head == "!agora":
                await self._cmd_agora(message)
            elif head == "!top":
                await self._cmd_top(message, args)
            elif head == "!comparar":
                await self._cmd_comparar(message, args)
            elif head == "!desconectar":
                await self._cmd_desconectar(message)
            elif head == "!minutos":
                await self._cmd_minutos(message, args)
            elif head == "!importar":
                await self._cmd_importar(message)
            elif head == "!curtidas":
                await self._cmd_curtidas(message, args)
            elif head in ("!generos", "!gêneros"):
                await self._cmd_generos(message, args)
        except Exception as exc:
            self._logger.error(
                "Falha ao executar comando do Spotify",
                extra={"context": {"command": head, "error": str(exc)}},
            )
            await message.channel.send(
                embed=_embed_error("❌ Deu ruim", f"```{type(exc).__name__}: {exc}```")
            )

    def _help_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🎧 Spotify do casal", color=SPOTIFY_GREEN)
        embed.add_field(
            name="Conexão",
            value=(
                "`!conectar` — recebe no DM o link para autorizar sua conta\n"
                "`!desconectar` — revoga a conexão e apaga seus dados guardados"
            ),
            inline=False,
        )
        embed.add_field(
            name="Consulta",
            value=(
                "`!agora` — o que cada um está ouvindo agora\n"
                "`!top [@pessoa] [período]` — top 10 do ranking do Spotify\n"
                "   períodos: `4-semanas` (padrão), `6-meses`, `1-ano`\n"
                "`!comparar [semana|passada]` — compara as escutas registradas pelo bot\n"
                "`!minutos [hoje|semana|mes|ano|tudo]` — tempo ouvido (padrão: mês)\n"
                "`!curtidas [período]` — músicas que os dois salvaram na biblioteca\n"
                "`!generos [período]` — perfil de gênero de cada um e o que se cruza"
            ),
            inline=False,
        )
        embed.add_field(
            name="Precisão do tempo ouvido",
            value=(
                "`!importar` — anexe o zip do Extended Streaming History do Spotify "
                "para ter os minutos **reais**, inclusive de antes do bot existir.\n"
                "Peça em Conta → Privacidade; chega por e-mail em até 30 dias."
            ),
            inline=False,
        )
        embed.add_field(
            name="Automático",
            value=(
                "Painel fixado atualizado a cada 60s\n"
                "Resumo semanal aos domingos, 20h de Brasília"
            ),
            inline=False,
        )
        embed.set_footer(
            text="Ranking do Spotify ≠ escutas registradas pelo bot. "
            "As escutas contam a partir da conexão."
        )
        return embed

    # ------------------------------------------------------------------ #
    # !conectar / !desconectar
    # ------------------------------------------------------------------ #

    async def _cmd_conectar(self, message: discord.Message) -> None:
        account = await self._store.get_account(message.author.id)
        url = await self._auth.build_auth_url(message.author.id)

        intro = (
            "Sua conexão expirou. Autorize de novo pelo link abaixo:"
            if account and account.needs_reauth
            else "Já está conectado — este link substitui a autorização atual:"
            if account
            else "Autorize sua conta do Spotify pelo link abaixo:"
        )

        embed = discord.Embed(
            title="🔗 Conectar Spotify",
            description=f"{intro}\n\n[Autorizar no Spotify]({url})\n\nO link vale por 15 minutos e é só seu.",
            color=SPOTIFY_GREEN,
        )
        embed.set_footer(text="O bot pede apenas: reprodução atual, histórico recente e seus tops.")

        try:
            await message.author.send(embed=embed)
            if not isinstance(message.channel, discord.DMChannel):
                await message.channel.send(
                    embed=_embed_info(f"{message.author.mention} te mandei o link no DM. 📬")
                )
        except discord.Forbidden:
            await message.channel.send(
                embed=_embed_error(
                    "❌ Não consegui te mandar DM",
                    "Libere mensagens diretas de membros do servidor e tente `!conectar` de novo.",
                )
            )

    async def _cmd_desconectar(self, message: discord.Message) -> None:
        account = await self._store.get_account(message.author.id)
        if account is None:
            await message.channel.send(embed=_embed_info("Você não tem nenhuma conta conectada."))
            return

        removed = await self._store.delete_account(message.author.id)
        self._last_playback.pop(message.author.id, None)
        self._progress.pop(message.author.id, None)
        self._panel_fingerprint = None
        self._panel_message = None

        self._logger.info(
            "Conta do Spotify desconectada",
            extra={"context": {"discord_user_id": str(message.author.id), "plays_removed": removed}},
        )
        await message.channel.send(
            embed=_embed_info(
                f"🗑️ Conexão revogada e **{removed}** escutas apagadas do banco.\n"
                "Para revogar também do lado do Spotify, remova o app em "
                "<https://www.spotify.com/account/apps/>."
            )
        )

    # ------------------------------------------------------------------ #
    # !agora
    # ------------------------------------------------------------------ #

    async def _cmd_agora(self, message: discord.Message) -> None:
        entries = await self._collect_playback()
        if not entries:
            await message.channel.send(
                embed=_embed_info("Nenhuma conta conectada ainda. Use `!conectar`.")
            )
            return
        await message.channel.send(
            embed=build_panel_embed(entries, datetime.now(self._tz))
        )

    # ------------------------------------------------------------------ #
    # !top
    # ------------------------------------------------------------------ #

    async def _cmd_top(self, message: discord.Message, args: List[str]) -> None:
        target_id = message.author.id
        period_raw: Optional[str] = None

        for arg in args:
            resolved = self._resolve_user_arg(arg, message)
            if resolved is not None:
                target_id = resolved
            else:
                period_raw = arg

        period = parse_period(period_raw)
        if period is None:
            await message.channel.send(
                embed=_embed_error(
                    "Período inválido",
                    "Use `4-semanas`, `6-meses` ou `1-ano`.",
                )
            )
            return

        if not self._is_allowed(target_id):
            await message.channel.send(
                embed=_embed_error("🔒 Bot privado", "Só dá para consultar as duas contas configuradas.")
            )
            return

        account = await self._store.get_account(target_id)
        display_name = await self._display_name(target_id, account)

        if account is None:
            await message.channel.send(
                embed=_embed_info(f"**{display_name}** ainda não conectou o Spotify.")
            )
            return
        if account.needs_reauth:
            await message.channel.send(
                embed=_embed_error(
                    "🔑 Autorização expirada",
                    f"**{display_name}** precisa rodar `!conectar` de novo.",
                )
            )
            return

        async with message.channel.typing():
            try:
                token = await self._auth.get_valid_access_token(account)
                tracks = await self._api.top_items(token, "tracks", period, limit=10)
                artists = await self._api.top_items(token, "artists", period, limit=10)
            except SpotifyForbidden as exc:
                # 403 nao e expiracao: nao marca reauth, senao vira loop de !conectar.
                self._logger.warning(
                    "Spotify negou o acesso a conta",
                    extra={"context": {"discord_user_id": str(target_id), "motivo": exc.motivo}},
                )
                await message.channel.send(embed=_embed_forbidden(display_name, exc))
                return
            except SpotifyAuthError:
                await self._store.mark_needs_reauth(target_id)
                await message.channel.send(
                    embed=_embed_error(
                        "🔑 Autorização expirada",
                        f"**{display_name}** precisa rodar `!conectar` de novo.",
                    )
                )
                return
            except SpotifyRateLimited as exc:
                await message.channel.send(
                    embed=_embed_error(
                        "⏳ Limite do Spotify",
                        f"Tente de novo em {int(exc.retry_after)}s.",
                    )
                )
                return
            except SpotifyUnavailable as exc:
                self._logger.warning(
                    "Spotify indisponivel no !top",
                    extra={"context": {"error": str(exc)}},
                )
                await message.channel.send(
                    embed=_embed_error("📡 Spotify indisponível", "Não deu para buscar o ranking agora.")
                )
                return

        await message.channel.send(
            embed=build_top_embed(display_name, period, tracks, artists)
        )

    def _resolve_user_arg(self, arg: str, message: discord.Message) -> Optional[int]:
        cleaned = arg.strip()
        if cleaned.lower() in ("eu", "me", "meu"):
            return message.author.id
        if cleaned.startswith("<@") and cleaned.endswith(">"):
            digits = cleaned.strip("<@!>")
            return int(digits) if digits.isdigit() else None
        if cleaned.isdigit() and len(cleaned) >= 17:
            return int(cleaned)
        return None

    # ------------------------------------------------------------------ #
    # !comparar
    # ------------------------------------------------------------------ #

    async def _cmd_comparar(self, message: discord.Message, args: List[str]) -> None:
        offset = parse_week_offset(args[0] if args else None)
        if offset is None:
            await message.channel.send(
                embed=_embed_error(
                    "Período inválido",
                    "Use `!comparar semana` ou `!comparar passada`.",
                )
            )
            return

        now = datetime.now(self._tz)
        start_ms, end_ms, start, end = week_bounds(now, offset)
        label = "semana atual" if offset == 0 else "semana anterior"
        range_label = f"{label} ({format_day_range(start, end)})"

        embed = await self._build_comparison(
            title="🆚 Comparativo da semana",
            range_label=range_label,
            start_ms=start_ms,
            end_ms=min(end_ms, to_ms(now)) if offset == 0 else end_ms,
        )
        await message.channel.send(embed=embed)

    async def _build_comparison(
        self, title: str, range_label: str, start_ms: int, end_ms: int
    ) -> discord.Embed:
        accounts = await self._store.list_accounts()
        sides: List[Dict[str, Any]] = []
        partial_note: Optional[str] = None

        for account in accounts:
            display_name = await self._display_name(account.discord_user_id, account)
            first_play = await self._store.first_play_at(account.discord_user_id)
            if first_play is not None and first_play > start_ms:
                partial_note = "Período parcial: alguém conectou depois do início da janela."

            escuta = await self._listening_for(account.discord_user_id, start_ms, end_ms)

            sides.append(
                {
                    "display_name": display_name,
                    "listening": escuta,
                    "play_count": await self._store.count_plays(
                        account.discord_user_id, start_ms, end_ms
                    ),
                    "tracks": await self._store.top_tracks(
                        account.discord_user_id, start_ms, end_ms, limit=5
                    ),
                    "artists": await self._store.top_artists(
                        account.discord_user_id, start_ms, end_ms, limit=5
                    ),
                }
            )

        shared: List[Dict[str, Any]] = []
        if len(accounts) >= 2:
            shared = await self._store.shared_tracks(
                accounts[0].discord_user_id,
                accounts[1].discord_user_id,
                start_ms,
                end_ms,
                limit=5,
            )
        elif len(accounts) == 1:
            partial_note = "Só uma conta conectada — sem comparação possível ainda."
        else:
            partial_note = "Nenhuma conta conectada ainda."

        return build_compare_embed(title, range_label, sides, shared, partial_note)

    # ------------------------------------------------------------------ #
    # !minutos
    # ------------------------------------------------------------------ #

    async def _cmd_minutos(self, message: discord.Message, args: List[str]) -> None:
        janela = parse_range(args[0] if args else None)
        if janela is None:
            await message.channel.send(
                embed=_embed_error(
                    "Período inválido",
                    "Use `hoje`, `semana`, `passada`, `mes`, `mes-passado`, `ano` ou `tudo`.",
                )
            )
            return

        agora = datetime.now(self._tz)
        start_ms, end_ms, rotulo = self._resolve_range(janela, agora)

        contas = await self._store.list_accounts()
        if not contas:
            await message.channel.send(
                embed=_embed_info("Nenhuma conta conectada ainda. Use `!conectar`.")
            )
            return

        embed = discord.Embed(
            title="⏱️ Tempo ouvido",
            description=rotulo,
            color=SPOTIFY_GREEN,
        )

        algum_estimado = False
        for conta in contas:
            nome = await self._display_name(conta.discord_user_id, conta)
            resultado = await self._listening_for(conta.discord_user_id, start_ms, end_ms)
            algum_estimado = algum_estimado or resultado["estimado_ms"] > 0

            linhas = [f"**{format_duration(resultado['total_ms'])}**"]
            linhas.append(describe_sources(resultado))

            primeira = await self._store.first_play_at(conta.discord_user_id)
            if primeira is not None and primeira > start_ms and resultado["exato_ms"] == 0:
                desde = datetime.fromtimestamp(primeira / 1000, self._tz)
                linhas.append(f"_dados a partir de {desde.strftime('%d/%m')}_")

            embed.add_field(name=nome, value="\n".join(linhas), inline=True)

        if algum_estimado:
            embed.set_footer(
                text="O Spotify não expõe minutos ouvidos: a parte estimada vem da "
                "duração das faixas. Use !importar para ter o número real."
            )
        else:
            embed.set_footer(text="Número real, vindo do histórico do Spotify.")

        await message.channel.send(embed=embed)

    def _resolve_range(self, janela: str, agora: datetime) -> Tuple[int, int, str]:
        if janela == "hoje":
            inicio_ms, fim_ms, inicio, _ = today_bounds(agora)
            return inicio_ms, min(fim_ms, to_ms(agora)), f"hoje ({inicio.strftime('%d/%m')})"
        if janela == "semana":
            inicio_ms, fim_ms, inicio, fim = week_bounds(agora, 0)
            return inicio_ms, min(fim_ms, to_ms(agora)), f"semana atual ({format_day_range(inicio, fim)})"
        if janela == "semana-passada":
            inicio_ms, fim_ms, inicio, fim = week_bounds(agora, -1)
            return inicio_ms, fim_ms, f"semana passada ({format_day_range(inicio, fim)})"
        if janela == "mes":
            inicio_ms, fim_ms, inicio, _ = month_bounds(agora, 0)
            return inicio_ms, min(fim_ms, to_ms(agora)), f"{inicio.strftime('%B de %Y')}"
        if janela == "mes-passado":
            inicio_ms, fim_ms, inicio, _ = month_bounds(agora, -1)
            return inicio_ms, fim_ms, f"{inicio.strftime('%B de %Y')}"
        if janela == "ano":
            inicio = agora.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            return to_ms(inicio), to_ms(agora), f"{inicio.year}"
        return 0, to_ms(agora), "todo o período registrado"

    async def _listening_for(
        self, discord_user_id: int, start_ms: int, end_ms: int
    ) -> Dict[str, Any]:
        """Combina as três fontes de tempo ouvido para a janela pedida."""
        return resolve_listening(
            imported=await self._store.raw_imported(discord_user_id, start_ms, end_ms),
            measured=await self._store.raw_measured(discord_user_id, start_ms, end_ms),
            plays=await self._store.raw_plays(discord_user_id, start_ms, end_ms),
            window_start=start_ms,
            window_end=end_ms,
        )

    # ------------------------------------------------------------------ #
    # !importar
    # ------------------------------------------------------------------ #

    async def _cmd_importar(self, message: discord.Message) -> None:
        if await self._store.get_account(message.author.id) is None:
            await message.channel.send(
                embed=_embed_info("Conecte sua conta primeiro com `!conectar`.")
            )
            return

        if not message.attachments:
            embed = discord.Embed(
                title="📥 Importar histórico do Spotify",
                description=(
                    "Anexe o arquivo junto do comando `!importar`.\n\n"
                    "**Como conseguir:**\n"
                    "1. Spotify → Conta → **Privacidade**\n"
                    "2. Marque **Extended streaming history** (não o histórico curto)\n"
                    "3. Confirme pelo e-mail; o zip chega em até 30 dias\n"
                    "4. Volte aqui e mande `!importar` com o zip anexado\n\n"
                    "Isso traz os minutos **reais** de cada faixa, inclusive de antes "
                    "de o bot existir. É o mesmo dado do Wrapped."
                ),
                color=SPOTIFY_GREEN,
            )
            embed.set_footer(text="Só você consegue importar para a sua própria conta.")
            await message.channel.send(embed=embed)
            return

        anexo = message.attachments[0]
        if anexo.size > MAX_IMPORT_BYTES:
            await message.channel.send(
                embed=_embed_error(
                    "Arquivo grande demais",
                    f"O anexo tem {anexo.size // (1024 * 1024)} MB e o limite é 25 MB. "
                    "Mande os `.json` de dentro do zip em partes.",
                )
            )
            return

        async with message.channel.typing():
            try:
                dados = await anexo.read()
            except Exception as exc:
                self._logger.error(
                    "Falha ao baixar anexo do import",
                    extra={"context": {"error": str(exc)}},
                )
                await message.channel.send(
                    embed=_embed_error("❌ Não consegui baixar o anexo", "Tente mandar de novo.")
                )
                return

            try:
                resultado = await asyncio.to_thread(parse_upload, dados, anexo.filename)
            except ImportError_ as exc:
                await message.channel.send(
                    embed=_embed_error("❌ Arquivo não serve", str(exc))
                )
                return
            except Exception as exc:
                self._logger.error(
                    "Erro inesperado ao ler arquivo de import",
                    extra={"context": {"error": str(exc)}},
                )
                await message.channel.send(
                    embed=_embed_error("❌ Não consegui ler o arquivo", f"```{type(exc).__name__}```")
                )
                return

            entradas = resultado["entradas"]
            if not entradas:
                await message.channel.send(
                    embed=_embed_info("O arquivo foi lido, mas não tinha nenhuma reprodução de música.")
                )
                return

            novas = await self._store.record_imported(message.author.id, entradas)

        stats = resumo_import(entradas)
        inicio = datetime.fromtimestamp(stats["inicio_ms"] / 1000, self._tz)
        fim = datetime.fromtimestamp(stats["fim_ms"] / 1000, self._tz)

        embed = discord.Embed(
            title="✅ Histórico importado",
            color=SPOTIFY_GREEN,
            description=(
                f"**{novas}** reproduções novas de **{stats['total']}** lidas "
                f"em {resultado['arquivos']} arquivo(s).\n"
                f"Período: {inicio.strftime('%d/%m/%Y')} a {fim.strftime('%d/%m/%Y')}\n"
                f"Tempo total no arquivo: **{format_duration(stats['ms'])}**"
            ),
        )
        if novas < stats["total"]:
            embed.add_field(
                name="Duplicatas",
                value=f"{stats['total'] - novas} já estavam no banco e foram ignoradas.",
                inline=False,
            )
        embed.set_footer(text="Esses minutos agora têm prioridade sobre a estimativa em !minutos.")

        self._logger.info(
            "Historico importado",
            extra={
                "context": {
                    "discord_user_id": str(message.author.id),
                    "lidas": stats["total"],
                    "novas": novas,
                    "arquivos": resultado["arquivos"],
                }
            },
        )
        await message.channel.send(embed=embed)

    # ------------------------------------------------------------------ #
    # !curtidas
    # ------------------------------------------------------------------ #

    async def _cmd_curtidas(self, message: discord.Message, args: List[str]) -> None:
        """Faixas que os dois ouviram E que os dois salvaram na biblioteca.

        Ouvir pode ser acaso de playlist; salvar e intencao. Por isso a interseccao
        das bibliotecas diz mais do que a das escutas.
        """
        janela = parse_range(args[0] if args else None)
        if janela is None:
            await message.channel.send(
                embed=_embed_error(
                    "Período inválido",
                    "Use `hoje`, `semana`, `passada`, `mes`, `ano` ou `tudo`.",
                )
            )
            return

        contas = await self._store.list_accounts()
        if len(contas) < 2:
            await message.channel.send(
                embed=_embed_info("Preciso das duas contas conectadas para comparar bibliotecas.")
            )
            return

        sem_escopo = [c for c in contas if not self._tem_escopo_biblioteca(c)]
        if sem_escopo:
            nomes = [await self._display_name(c.discord_user_id, c) for c in sem_escopo]
            await message.channel.send(
                embed=_embed_error(
                    "🔑 Falta permissão de biblioteca",
                    f"{' e '.join(nomes)} conectou antes deste comando existir.\n"
                    "Rode `!conectar` de novo para autorizar a leitura da biblioteca "
                    "(`user-library-read`). O bot continua sem poder alterar nada — só ler.",
                )
            )
            return

        agora = datetime.now(self._tz)
        start_ms, end_ms, rotulo = self._resolve_range(janela, agora)

        async with message.channel.typing():
            candidatas = await self._store.shared_track_ids(
                contas[0].discord_user_id, contas[1].discord_user_id, start_ms, end_ms, limit=100
            )
            if not candidatas:
                await message.channel.send(
                    embed=_embed_info(
                        f"Vocês não ouviram nenhuma música em comum em {rotulo}."
                    )
                )
                return

            ids = [c["track_id"] for c in candidatas]
            salvos: Dict[int, Dict[str, bool]] = {}

            for conta in contas:
                try:
                    token = await self._auth.get_valid_access_token(conta)
                    salvos[conta.discord_user_id] = await self._api.library_contains(token, ids)
                except SpotifyForbidden as exc:
                    nome = await self._display_name(conta.discord_user_id, conta)
                    await message.channel.send(embed=_embed_forbidden(nome, exc))
                    return
                except SpotifyAuthError:
                    await self._store.mark_needs_reauth(conta.discord_user_id)
                    nome = await self._display_name(conta.discord_user_id, conta)
                    await message.channel.send(
                        embed=_embed_error(
                            "🔑 Autorização expirada", f"**{nome}** precisa rodar `!conectar`."
                        )
                    )
                    return
                except (SpotifyRateLimited, SpotifyUnavailable) as exc:
                    self._logger.warning(
                        "Falha ao consultar biblioteca",
                        extra={"context": {"error": str(exc)}},
                    )
                    await message.channel.send(
                        embed=_embed_error("📡 Spotify indisponível", "Tente de novo daqui a pouco.")
                    )
                    return

        a, b = contas[0].discord_user_id, contas[1].discord_user_id
        ambos = [
            c for c in candidatas
            if salvos[a].get(c["track_id"]) and salvos[b].get(c["track_id"])
        ]

        embed = discord.Embed(
            title="💚 Curtidas em comum",
            description=f"Músicas que vocês dois salvaram na biblioteca — {rotulo}",
            color=SPOTIFY_GREEN,
        )

        if ambos:
            linhas = []
            for faixa in ambos[:15]:
                titulo = faixa["track_name"]
                if faixa.get("track_url"):
                    titulo = f"[{titulo}]({faixa['track_url']})"
                linhas.append(f"• {titulo} — {faixa['artists']}")
            embed.add_field(name=f"{len(ambos)} em comum", value=fit_field(linhas), inline=False)
        else:
            embed.add_field(
                name="Nenhuma ainda",
                value="Vocês ouviram as mesmas músicas, mas nenhuma está salva nas duas bibliotecas.",
                inline=False,
            )

        so_a = sum(1 for c in candidatas if salvos[a].get(c["track_id"]) and not salvos[b].get(c["track_id"]))
        so_b = sum(1 for c in candidatas if salvos[b].get(c["track_id"]) and not salvos[a].get(c["track_id"]))
        nome_a = await self._display_name(a, contas[0])
        nome_b = await self._display_name(b, contas[1])

        embed.add_field(
            name="Salvou só um",
            value=f"{nome_a}: **{so_a}** · {nome_b}: **{so_b}**",
            inline=False,
        )
        embed.set_footer(
            text=f"Olhei as {len(candidatas)} músicas que vocês mais ouviram em comum no período."
        )
        await message.channel.send(embed=embed)

    def _tem_escopo_biblioteca(self, conta: SpotifyAccount) -> bool:
        return SCOPE_BIBLIOTECA in (conta.scope or "")

    # ------------------------------------------------------------------ #
    # !generos
    # ------------------------------------------------------------------ #

    async def _cmd_generos(self, message: discord.Message, args: List[str]) -> None:
        janela = parse_range(args[0] if args else None)
        if janela is None:
            await message.channel.send(
                embed=_embed_error("Período inválido", "Use `semana`, `mes`, `ano` ou `tudo`.")
            )
            return

        contas = await self._store.list_accounts()
        if not contas:
            await message.channel.send(embed=_embed_info("Ninguém conectado ainda."))
            return

        agora = datetime.now(self._tz)
        start_ms, end_ms, rotulo = self._resolve_range(janela, agora)

        perfis = []
        async with message.channel.typing():
            for conta in contas:
                nome = await self._display_name(conta.discord_user_id, conta)
                perfil = await self._perfil_de_genero(conta, start_ms, end_ms)
                perfil["nome"] = nome
                perfis.append(perfil)

        embed = discord.Embed(
            title="🎨 Perfil de gênero",
            description=f"A partir dos artistas mais ouvidos — {rotulo}",
            color=SPOTIFY_GREEN,
        )

        classificados = 0
        total_artistas = 0

        for perfil in perfis:
            classificados += perfil["com_genero"]
            total_artistas += perfil["artistas"]

            if perfil["generos"]:
                linhas = [
                    f"`{pct:>3}%` {genero}" for genero, pct in perfil["generos"][:5]
                ]
                valor = "\n".join(linhas)
            elif perfil["artistas"] == 0:
                valor = "_Nada registrado neste período._"
            else:
                valor = "_O Spotify não classificou nenhum destes artistas._"
            embed.add_field(name=perfil["nome"], value=valor, inline=True)

        if len(perfis) >= 2:
            comuns = self._generos_em_comum(perfis[0], perfis[1])
            embed.add_field(
                name="🤝 Onde vocês se encontram",
                value=fit_field([f"• {g}" for g in comuns[:8]]) if comuns
                else "_Nenhum gênero em comum entre os artistas classificados._",
                inline=False,
            )

        if total_artistas:
            cobertura = round(100 * classificados / total_artistas)
            embed.set_footer(
                text=f"{cobertura}% dos artistas tinham gênero classificado. "
                "O Spotify marcou esse campo como descontinuado e devolve vazio "
                "para muitos artistas — o que falta não é erro do bot."
            )
        await message.channel.send(embed=embed)

    async def _perfil_de_genero(
        self, conta: SpotifyAccount, start_ms: int, end_ms: int
    ) -> Dict[str, Any]:
        """Pondera os generos pelo numero de escutas de cada artista."""
        artistas = await self._store.top_artist_ids(
            conta.discord_user_id, start_ms, end_ms, limit=40
        )
        if not artistas:
            return {"generos": [], "artistas": 0, "com_genero": 0, "conjunto": set()}

        ids = [a["artist_id"] for a in artistas]
        cache = await self._store.get_cached_genres(ids)
        faltando = [aid for aid in ids if aid not in cache]

        if faltando:
            try:
                token = await self._auth.get_valid_access_token(conta)
            except (SpotifyAuthError, SpotifyForbidden):
                token = None

            if token:
                for artist_id in faltando:
                    try:
                        dados = await self._api.artist(token, artist_id)
                    except (SpotifyAuthError, SpotifyForbidden, SpotifyRateLimited, SpotifyUnavailable) as exc:
                        self._logger.warning(
                            "Falha ao buscar generos do artista",
                            extra={"context": {"artist_id": artist_id, "error": str(exc)}},
                        )
                        break
                    generos = (dados or {}).get("genres") or []
                    cache[artist_id] = generos
                    # Guarda inclusive o vazio, para nao repetir a chamada.
                    await self._store.cache_genres(
                        artist_id, (dados or {}).get("name"), generos, int(time.time())
                    )

        pontos: Dict[str, int] = {}
        com_genero = 0
        for artista in artistas:
            generos = cache.get(artista["artist_id"]) or []
            if generos:
                com_genero += 1
            for genero in generos:
                pontos[genero] = pontos.get(genero, 0) + artista["plays"]

        total = sum(pontos.values())
        ordenados = sorted(pontos.items(), key=lambda item: -item[1])
        generos = [
            (genero, round(100 * peso / total)) for genero, peso in ordenados
        ] if total else []

        return {
            "generos": generos,
            "artistas": len(artistas),
            "com_genero": com_genero,
            "conjunto": set(pontos),
        }

    @staticmethod
    def _generos_em_comum(perfil_a: Dict[str, Any], perfil_b: Dict[str, Any]) -> List[str]:
        comuns = perfil_a["conjunto"] & perfil_b["conjunto"]
        if not comuns:
            return []
        peso = {g: p for g, p in perfil_a["generos"]}
        return sorted(comuns, key=lambda g: -peso.get(g, 0))

    # ------------------------------------------------------------------ #
    # Reproducao atual (painel e !agora)
    # ------------------------------------------------------------------ #

    async def _collect_playback(self) -> List[Dict[str, Any]]:
        accounts = await self._store.list_accounts()
        entries: List[Dict[str, Any]] = []

        for account in accounts:
            display_name = await self._display_name(account.discord_user_id, account)
            playback = await self._playback_for(account)
            entries.append(
                {
                    "discord_user_id": account.discord_user_id,
                    "display_name": display_name,
                    "playback": playback,
                }
            )
        return entries

    async def _playback_for(self, account: SpotifyAccount) -> Dict[str, Any]:
        if account.needs_reauth:
            return {"status": "disconnected", "text": "Autorização expirada — rode `!conectar`"}

        try:
            token = await self._auth.get_valid_access_token(account)
            state = await self._api.currently_playing(token)
        except SpotifyForbidden as exc:
            self._logger.warning(
                "Spotify negou o acesso a conta no painel",
                extra={
                    "context": {
                        "discord_user_id": str(account.discord_user_id),
                        "motivo": exc.motivo,
                    }
                },
            )
            if exc.conta_nao_cadastrada:
                return {"status": "forbidden", "text": "Conta não liberada no app do Spotify"}
            return {"status": "forbidden", "text": "Spotify negou o acesso"}
        except SpotifyAuthError:
            await self._store.mark_needs_reauth(account.discord_user_id)
            return {"status": "disconnected", "text": "Autorização expirada — rode `!conectar`"}
        except SpotifyRateLimited as exc:
            self._rate_limited_until = time.time() + exc.retry_after
            return self._stale_playback(account.discord_user_id)
        except SpotifyUnavailable as exc:
            self._logger.warning(
                "Spotify indisponivel ao ler reproducao atual",
                extra={"context": {"error": str(exc), "discord_user_id": str(account.discord_user_id)}},
            )
            return self._stale_playback(account.discord_user_id)

        await self._sample_progress(account.discord_user_id, state)

        playback = describe_playback(state)
        if playback["status"] in ("playing", "paused"):
            self._last_playback[account.discord_user_id] = (playback, datetime.now(self._tz))
        return playback

    async def _sample_progress(
        self, discord_user_id: int, state: Optional[Dict[str, Any]]
    ) -> None:
        """Camada 2 do tempo ouvido: mede o avanco real de `progress_ms`.

        Roda junto do tick do painel, que ja consulta o player, entao nao custa
        requisicao extra. Pausa nao avanca o progresso, e faixa pulada so acumula o
        que tocou de fato.
        """
        agora = int(time.time() * 1000)
        anterior = self._progress.get(discord_user_id)

        item = (state or {}).get("item") or {}
        track_id = item.get("id")
        progresso = (state or {}).get("progress_ms")

        if not track_id or progresso is None or (state or {}).get("currently_playing_type") != "track":
            self._progress.pop(discord_user_id, None)  # fecha a sessao; ja esta persistida
            return

        progresso = int(progresso)

        mesma_faixa = anterior is not None and anterior["track_id"] == track_id
        recomecou = mesma_faixa and progresso < anterior["progress_ms"]

        if not mesma_faixa or recomecou:
            # Sessao nova. O progresso atual ja indica o quanto tocou desta faixa.
            sessao = {
                "track_id": track_id,
                "progress_ms": progresso,
                "wall_ms": agora,
                "session_start_ms": agora - progresso,
                "acumulado_ms": progresso,
            }
        else:
            avanco = progresso - anterior["progress_ms"]
            # O avanco nunca pode passar do tempo de relogio decorrido: isso descarta
            # pulos para frente na faixa e protege de amostra fora de ordem.
            teto = max(0, agora - anterior["wall_ms"])
            avanco = max(0, min(avanco, teto))
            sessao = {
                "track_id": track_id,
                "progress_ms": progresso,
                "wall_ms": agora,
                "session_start_ms": anterior["session_start_ms"],
                "acumulado_ms": anterior["acumulado_ms"] + avanco,
            }

        self._progress[discord_user_id] = sessao

        if sessao["acumulado_ms"] > 0:
            try:
                await self._store.upsert_measured(
                    discord_user_id=discord_user_id,
                    track_id=track_id,
                    started_ms=sessao["session_start_ms"],
                    ended_ms=agora,
                    ms_played=sessao["acumulado_ms"],
                )
            except Exception as exc:
                self._logger.warning(
                    "Falha ao gravar tempo medido",
                    extra={"context": {"error": str(exc), "discord_user_id": str(discord_user_id)}},
                )

    def _stale_playback(self, discord_user_id: int) -> Dict[str, Any]:
        cached = self._last_playback.get(discord_user_id)
        if cached is None:
            return {"status": "error", "text": "Dados indisponíveis"}
        playback, seen_at = cached
        stale = dict(playback)
        stale["status"] = "error"
        stale["text"] = f"Dados indisponíveis · último às {seen_at.strftime('%H:%M')}"
        return stale

    async def _display_name(
        self, discord_user_id: int, account: Optional[SpotifyAccount] = None
    ) -> str:
        if self._discord is not None:
            guild = self._discord.get_guild(self._guild_id)
            if guild is not None:
                member = guild.get_member(discord_user_id)
                if member is not None:
                    return member.display_name
            user = self._discord.get_user(discord_user_id)
            if user is not None:
                return user.display_name
        if account is not None and account.display_name:
            return account.display_name
        return f"Usuário {discord_user_id}"

    # ------------------------------------------------------------------ #
    # Painel fixado
    # ------------------------------------------------------------------ #

    async def refresh_panel(self) -> None:
        if self._discord is None or time.time() < self._rate_limited_until:
            return

        channel = self._discord.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self._discord.fetch_channel(self._channel_id)
            except Exception as exc:
                self._logger.error(
                    "Canal do painel inacessivel",
                    extra={"context": {"channel_id": str(self._channel_id), "error": str(exc)}},
                )
                return

        entries = await self._collect_playback()
        if not entries:
            return

        fingerprint = panel_fingerprint(entries)
        message = await self._get_panel_message(channel)

        if message is not None and fingerprint == self._panel_fingerprint:
            return

        embed = build_panel_embed(entries, datetime.now(self._tz))

        if message is None:
            message = await channel.send(embed=embed)
            self._panel_message = message
            await self._store.set_value(PANEL_MESSAGE_KEY, str(message.id))
            try:
                await message.pin()
            except discord.HTTPException as exc:
                self._logger.warning(
                    "Nao consegui fixar o painel",
                    extra={"context": {"error": str(exc)}},
                )
        else:
            try:
                await message.edit(embed=embed)
            except discord.NotFound:
                self._logger.info("Painel sumiu durante a edicao, sera recriado")
                self._panel_message = None
                await self._store.delete_value(PANEL_MESSAGE_KEY)
                self._panel_fingerprint = None
                return

        self._panel_fingerprint = fingerprint

    async def _get_panel_message(self, channel: Any) -> Optional[discord.Message]:
        if self._panel_message is not None:
            return self._panel_message

        raw_id = await self._store.get_value(PANEL_MESSAGE_KEY)
        if not raw_id:
            return None
        try:
            self._panel_message = await channel.fetch_message(int(raw_id))
            return self._panel_message
        except discord.NotFound:
            self._logger.info("Painel anterior nao existe mais, sera recriado")
            await self._store.delete_value(PANEL_MESSAGE_KEY)
            return None
        except discord.HTTPException as exc:
            self._logger.warning(
                "Falha ao recuperar o painel",
                extra={"context": {"error": str(exc)}},
            )
            return None

    # ------------------------------------------------------------------ #
    # Coleta do historico recente
    # ------------------------------------------------------------------ #

    async def sync_recent_plays(self) -> None:
        if time.time() < self._rate_limited_until:
            return

        for account in await self._store.list_accounts():
            if account.needs_reauth:
                continue
            try:
                await self._sync_account(account)
            except SpotifyForbidden as exc:
                self._logger.warning(
                    "Coleta bloqueada: Spotify negou o acesso a conta",
                    extra={
                        "context": {
                            "discord_user_id": str(account.discord_user_id),
                            "motivo": exc.motivo,
                        }
                    },
                )
            except SpotifyAuthError:
                await self._store.mark_needs_reauth(account.discord_user_id)
                self._logger.warning(
                    "Conta precisa reconectar durante a coleta",
                    extra={"context": {"discord_user_id": str(account.discord_user_id)}},
                )
            except SpotifyRateLimited as exc:
                self._rate_limited_until = time.time() + exc.retry_after
                self._logger.warning(
                    "Coleta pausada por rate limit",
                    extra={"context": {"retry_after": exc.retry_after}},
                )
                return
            except SpotifyUnavailable as exc:
                self._logger.warning(
                    "Spotify indisponivel durante a coleta",
                    extra={"context": {"error": str(exc), "discord_user_id": str(account.discord_user_id)}},
                )

    async def _sync_account(self, account: SpotifyAccount) -> None:
        token = await self._auth.get_valid_access_token(account)
        cursor = account.last_played_at_ms
        total_new = 0
        highest = cursor or 0

        for _ in range(MAX_RECENT_PAGES):
            page = await self._api.recently_played(token, after_ms=cursor, limit=50)
            items = page.get("items") or []
            if not items:
                break

            plays: List[Play] = []
            for item in items:
                track = item.get("track") or {}
                track_id = track.get("id")
                played_at = item.get("played_at")
                if not track_id or not played_at:
                    continue

                played_ms = self._iso_to_ms(played_at)
                if played_ms is None:
                    continue

                images = (track.get("album") or {}).get("images") or []
                plays.append(
                    Play(
                        discord_user_id=account.discord_user_id,
                        track_id=track_id,
                        played_at_ms=played_ms,
                        track_name=track.get("name") or "Faixa desconhecida",
                        artists=", ".join(
                            a.get("name", "") for a in track.get("artists", []) if a.get("name")
                        )
                        or "Artista desconhecido",
                        artist_ids=[a.get("id") for a in track.get("artists", []) if a.get("id")],
                        album_name=(track.get("album") or {}).get("name"),
                        album_image=images[0].get("url") if images else None,
                        track_url=(track.get("external_urls") or {}).get("spotify"),
                    )
                )
                highest = max(highest, played_ms)

            total_new += await self._store.record_plays(plays)

            next_after = (page.get("cursors") or {}).get("after")
            if len(items) < 50 or not next_after:
                break
            cursor = int(next_after)

        now = int(time.time())
        if highest:
            await self._store.set_sync_cursor(account.discord_user_id, highest, now)
        else:
            await self._store.touch_sync(account.discord_user_id, now)

        if total_new:
            self._logger.info(
                "Escutas registradas",
                extra={
                    "context": {
                        "discord_user_id": str(account.discord_user_id),
                        "new_plays": total_new,
                    }
                },
            )

    @staticmethod
    def _iso_to_ms(value: str) -> Optional[int]:
        try:
            normalized = value.replace("Z", "+00:00")
            return int(datetime.fromisoformat(normalized).timestamp() * 1000)
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    # Resumo semanal
    # ------------------------------------------------------------------ #

    async def publish_weekly_summary(self, force: bool = False) -> None:
        if self._discord is None:
            return

        now = datetime.now(self._tz)
        if not force and now.weekday() != 6:  # 6 = domingo
            return

        key = f"{WEEKLY_SUMMARY_PREFIX}{iso_week_key(now)}"
        if not force and await self._store.get_value(key) is not None:
            self._logger.info(
                "Resumo semanal ja publicado nesta semana",
                extra={"context": {"week": iso_week_key(now)}},
            )
            return

        accounts = await self._store.list_accounts()
        if not accounts:
            self._logger.info("Sem contas conectadas, resumo semanal nao publicado")
            return

        channel = self._discord.get_channel(self._channel_id)
        if channel is None:
            try:
                channel = await self._discord.fetch_channel(self._channel_id)
            except Exception as exc:
                self._logger.error(
                    "Canal do resumo inacessivel",
                    extra={"context": {"error": str(exc)}},
                )
                return

        start_ms, end_ms, start, end = last_7_days(now)
        embed = await self._build_comparison(
            title="📅 Resumo da semana",
            range_label=f"últimos 7 dias ({format_day_range(start, end)})",
            start_ms=start_ms,
            end_ms=end_ms,
        )

        message = await channel.send(embed=embed)
        await self._store.set_value(key, str(message.id))
        self._logger.info(
            "Resumo semanal publicado",
            extra={"context": {"week": iso_week_key(now), "message_id": str(message.id)}},
        )
