"""Janelas de tempo no fuso de Brasilia e formatacao dos embeds do Spotify."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import discord

SPOTIFY_GREEN = discord.Color.from_rgb(29, 185, 84)

# Rotulos dos rankings do proprio Spotify (nao das escutas registradas pelo bot).
PERIOD_ALIASES: Dict[str, str] = {
    "4-semanas": "short_term",
    "4semanas": "short_term",
    "short": "short_term",
    "short_term": "short_term",
    "mes": "short_term",
    "mês": "short_term",
    "6-meses": "medium_term",
    "6meses": "medium_term",
    "medium": "medium_term",
    "medium_term": "medium_term",
    "semestre": "medium_term",
    "1-ano": "long_term",
    "1ano": "long_term",
    "ano": "long_term",
    "long": "long_term",
    "long_term": "long_term",
}

PERIOD_LABELS: Dict[str, str] = {
    "short_term": "últimas ~4 semanas",
    "medium_term": "últimos ~6 meses",
    "long_term": "último ~1 ano",
}

# Janelas aceitas por !minutos
RANGE_ALIASES: Dict[str, str] = {
    "hoje": "hoje",
    "semana": "semana",
    "essa": "semana",
    "atual": "semana",
    "passada": "semana-passada",
    "semana-passada": "semana-passada",
    "mes": "mes",
    "mês": "mes",
    "mes-passado": "mes-passado",
    "mês-passado": "mes-passado",
    "ano": "ano",
    "tudo": "tudo",
    "total": "tudo",
}


def parse_range(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return "mes"
    return RANGE_ALIASES.get(raw.strip().lower())


WEEK_ALIASES: Dict[str, int] = {
    "semana": 0,
    "atual": 0,
    "essa": 0,
    "esta": 0,
    "semana-atual": 0,
    "passada": -1,
    "anterior": -1,
    "semana-passada": -1,
    "semana-anterior": -1,
}


FIELD_LIMIT = 1024  # limite do Discord por campo de embed


def fit_field(lines: List[str], limit: int = FIELD_LIMIT) -> str:
    """Junta as linhas ate o limite do Discord, avisando quantas ficaram de fora."""
    if not lines:
        return ""

    kept: List[str] = []
    size = 0
    for index, line in enumerate(lines):
        addition = len(line) + (1 if kept else 0)
        remaining = len(lines) - index
        suffix = f"\n_… +{remaining} não couberam_"
        reserve = len(suffix) if remaining > 0 else 0

        if size + addition + reserve > limit:
            kept.append(f"_… +{remaining} não couberam_")
            return "\n".join(kept)

        kept.append(line)
        size += addition

    return "\n".join(kept)


def parse_period(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return "short_term"
    return PERIOD_ALIASES.get(raw.strip().lower())


def parse_week_offset(raw: Optional[str]) -> Optional[int]:
    if not raw:
        return 0
    return WEEK_ALIASES.get(raw.strip().lower())


# ---------------------------------------------------------------------- #
# Janelas de tempo
# ---------------------------------------------------------------------- #


def to_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def week_bounds(now: datetime, offset: int = 0) -> Tuple[int, int, datetime, datetime]:
    """Semana de segunda 00:00 ate a segunda seguinte, no fuso de `now`.

    offset 0 = semana atual, -1 = semana anterior. Devolve (inicio_ms, fim_ms, inicio, fim).
    """
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    monday = start_of_day - timedelta(days=start_of_day.weekday())
    start = monday + timedelta(weeks=offset)
    end = start + timedelta(weeks=1)
    return to_ms(start), to_ms(end), start, end


def month_bounds(now: datetime, offset: int = 0) -> Tuple[int, int, datetime, datetime]:
    """Mes corrente (offset 0) ou anterior (-1), do dia 1 as 00:00 no fuso de `now`."""
    inicio = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    for _ in range(abs(offset)):
        if offset < 0:
            inicio = (inicio - timedelta(days=1)).replace(day=1)
        else:
            inicio = (inicio + timedelta(days=32)).replace(day=1)

    fim = (inicio + timedelta(days=32)).replace(day=1)
    return to_ms(inicio), to_ms(fim), inicio, fim


def today_bounds(now: datetime) -> Tuple[int, int, datetime, datetime]:
    inicio = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fim = inicio + timedelta(days=1)
    return to_ms(inicio), to_ms(fim), inicio, fim


def last_7_days(now: datetime) -> Tuple[int, int, datetime, datetime]:
    start = now - timedelta(days=7)
    return to_ms(start), to_ms(now), start, now


def iso_week_key(moment: datetime) -> str:
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def format_day_range(start: datetime, end: datetime) -> str:
    end_display = end - timedelta(seconds=1)
    return f"{start.strftime('%d/%m')} a {end_display.strftime('%d/%m')}"


# ---------------------------------------------------------------------- #
# Reproducao atual
# ---------------------------------------------------------------------- #


def describe_playback(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normaliza a resposta de currently-playing em algo pronto para o embed."""
    if state is None:
        return {"status": "idle", "text": "Sem reprodução ativa"}

    item = state.get("item")
    playing_type = state.get("currently_playing_type")

    if playing_type == "ad":
        return {"status": "ad", "text": "Anúncio tocando"}
    if item is None or playing_type not in ("track", None):
        return {"status": "unsupported", "text": "Tocando algo que não é música"}

    artists = ", ".join(a.get("name", "") for a in item.get("artists", []) if a.get("name"))
    images = (item.get("album") or {}).get("images") or []
    url = (item.get("external_urls") or {}).get("spotify")

    return {
        "status": "playing" if state.get("is_playing") else "paused",
        "text": "Tocando agora" if state.get("is_playing") else "Pausado",
        "track_name": item.get("name") or "Faixa desconhecida",
        "artists": artists or "Artista desconhecido",
        "album": (item.get("album") or {}).get("name"),
        "image": images[0].get("url") if images else None,
        "url": url,
    }


def playback_line(playback: Dict[str, Any]) -> str:
    status = playback["status"]
    if status in ("idle", "ad", "unsupported", "error", "disconnected", "forbidden"):
        return f"_{playback['text']}_"

    icon = "▶️" if status == "playing" else "⏸️"
    name = playback["track_name"]
    title = f"[{name}]({playback['url']})" if playback.get("url") else f"**{name}**"
    return f"{icon} {title}\n{playback['artists']}"


# ---------------------------------------------------------------------- #
# Embeds
# ---------------------------------------------------------------------- #


def build_panel_embed(entries: List[Dict[str, Any]], updated_at: datetime) -> discord.Embed:
    embed = discord.Embed(
        title="🎧 Ouvindo agora",
        color=SPOTIFY_GREEN,
    )

    cover: Optional[str] = None
    for entry in entries:
        playback = entry["playback"]
        embed.add_field(
            name=entry["display_name"][:256],
            value=playback_line(playback)[:FIELD_LIMIT],
            inline=False,
        )
        if cover is None and playback.get("image"):
            cover = playback["image"]

    if cover:
        embed.set_thumbnail(url=cover)

    embed.set_footer(text=f"Atualizado às {updated_at.strftime('%H:%M')} (Brasília)")
    return embed


def panel_fingerprint(entries: List[Dict[str, Any]]) -> str:
    """Identidade do conteudo do painel, para editar so quando algo muda de fato."""
    parts = []
    for entry in entries:
        playback = entry["playback"]
        parts.append(
            "|".join(
                [
                    str(entry["discord_user_id"]),
                    playback.get("status", ""),
                    playback.get("track_name", ""),
                    playback.get("artists", ""),
                ]
            )
        )
    return "||".join(parts)


def build_top_embed(
    display_name: str,
    period: str,
    tracks: List[Dict[str, Any]],
    artists: List[Dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 Top de {display_name}",
        description=f"Ranking do Spotify — {PERIOD_LABELS[period]}",
        color=SPOTIFY_GREEN,
    )

    if tracks:
        lines = []
        for i, track in enumerate(tracks, start=1):
            names = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
            url = (track.get("external_urls") or {}).get("spotify")
            title = f"[{track.get('name')}]({url})" if url else track.get("name")
            lines.append(f"`{i:>2}.` {title} — {names}")
        embed.add_field(name="🎵 Músicas", value=fit_field(lines), inline=False)
    else:
        embed.add_field(name="🎵 Músicas", value="_Spotify não devolveu ranking para este período._", inline=False)

    if artists:
        lines = []
        for i, artist in enumerate(artists, start=1):
            url = (artist.get("external_urls") or {}).get("spotify")
            name = artist.get("name")
            title = f"[{name}]({url})" if url else name
            lines.append(f"`{i:>2}.` {title}")
        embed.add_field(name="🎤 Artistas", value=fit_field(lines), inline=False)
    else:
        embed.add_field(name="🎤 Artistas", value="_Spotify não devolveu ranking para este período._", inline=False)

    embed.set_footer(text="Ranking calculado pelo Spotify, não pelas escutas registradas pelo bot.")
    return embed


def build_compare_embed(
    title: str,
    range_label: str,
    sides: List[Dict[str, Any]],
    shared: List[Dict[str, Any]],
    partial_note: Optional[str] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=f"Escutas registradas pelo bot — {range_label}",
        color=SPOTIFY_GREEN,
    )

    for side in sides:
        lines = []

        escuta = side.get("listening")
        if escuta and escuta["total_ms"] > 0:
            from .spotify_listening import format_duration

            marca = "" if escuta["preciso"] else "≈ "
            lines.append(f"⏱️ {marca}**{format_duration(escuta['total_ms'])}**")

        lines.append(f"**{side['play_count']}** reproduções registradas")

        if side["tracks"]:
            lines.append("")
            lines.append("**Músicas**")
            for i, track in enumerate(side["tracks"], start=1):
                title_text = track["track_name"]
                if track.get("track_url"):
                    title_text = f"[{title_text}]({track['track_url']})"
                lines.append(f"`{i}.` {title_text} — {track['artists']} (×{track['plays']})")

        if side["artists"]:
            lines.append("")
            lines.append("**Artistas**")
            for i, artist in enumerate(side["artists"], start=1):
                lines.append(f"`{i}.` {artist['artist']} (×{artist['plays']})")

        if not side["tracks"] and not side["artists"]:
            lines.append("_Nada registrado neste período._")

        embed.add_field(name=side["display_name"], value=fit_field(lines), inline=True)

    if shared:
        lines = []
        for track in shared:
            title_text = track["track_name"]
            if track.get("track_url"):
                title_text = f"[{title_text}]({track['track_url']})"
            lines.append(f"• {title_text} — {track['artists']}")
        embed.add_field(name="💚 Ouviram os dois", value=fit_field(lines), inline=False)
    else:
        embed.add_field(
            name="💚 Ouviram os dois",
            value="_Nenhuma música em comum neste período._",
            inline=False,
        )

    footer = "Contagem começa na data da conexão. O histórico recente do Spotify pode ter lacunas."
    if partial_note:
        footer = f"{partial_note} {footer}"
    embed.set_footer(text=footer)
    return embed
