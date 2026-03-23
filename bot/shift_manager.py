import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Sao_Paulo"


def _tz(tz_name: Optional[str] = None) -> ZoneInfo:
    return ZoneInfo(tz_name or DEFAULT_TZ)


def parse_entries(raw: str) -> List[str]:
    if not raw or not raw.strip():
        return []
    try:
        entries = json.loads(raw)
        if isinstance(entries, list):
            return [str(e) for e in entries]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def serialize_entries(entries: List[str]) -> str:
    return json.dumps(entries)


def is_shift_open(entries: List[str]) -> bool:
    return len(entries) % 2 != 0


def current_status(entries: List[str]) -> str:
    if not entries:
        return "Sem entradas"
    return "Trabalhando" if is_shift_open(entries) else "Pausa / Encerrado"


def now_timestamp(tz_name: Optional[str] = None) -> str:
    return datetime.now(_tz(tz_name)).strftime("%H:%M")


def now_local(tz_name: Optional[str] = None) -> datetime:
    return datetime.now(_tz(tz_name))


def calculate_summary(entries: List[str], tz_name: Optional[str] = None) -> Dict[str, Any]:
    """Calculates work periods, pauses, and totals from entry timestamps."""
    local_tz = _tz(tz_name)
    parsed = _parse_times(entries, local_tz)
    if not parsed:
        return {"work_periods": [], "pauses": [], "total_work_min": 0, "total_pause_min": 0}

    work_periods: List[Tuple[str, str, int]] = []
    pauses: List[Tuple[str, str, int]] = []

    for i in range(0, len(parsed), 2):
        start = parsed[i]
        if i + 1 < len(parsed):
            end = parsed[i + 1]
            mins = _diff_minutes(start, end)
            work_periods.append((entries[i], entries[i + 1], mins))
        else:
            mins = _diff_minutes(start, datetime.now(local_tz))
            work_periods.append((entries[i], "agora", mins))

    for i in range(1, len(parsed) - 1, 2):
        pause_start = parsed[i]
        pause_end = parsed[i + 1]
        mins = _diff_minutes(pause_start, pause_end)
        pauses.append((entries[i], entries[i + 1], mins))

    total_work = sum(p[2] for p in work_periods)
    total_pause = sum(p[2] for p in pauses)

    return {
        "work_periods": work_periods,
        "pauses": pauses,
        "total_work_min": total_work,
        "total_pause_min": total_pause,
    }


def format_duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}min"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h{mins:02d}min"


def build_history_line(entries: List[str]) -> str:
    if not entries:
        return "-"
    parts = list(entries)
    if is_shift_open(entries):
        parts.append("**agora**")
    return " > ".join(parts)


def _parse_times(entries: List[str], local_tz: Optional[ZoneInfo] = None) -> List[datetime]:
    tz = local_tz or _tz()
    today = datetime.now(tz).date()
    result = []
    for e in entries:
        try:
            t = datetime.strptime(e, "%H:%M").replace(
                year=today.year, month=today.month, day=today.day,
                tzinfo=tz,
            )
            result.append(t)
        except ValueError:
            continue
    return result


def _diff_minutes(start: datetime, end: datetime) -> int:
    delta = end - start
    return max(0, int(delta.total_seconds() / 60))


def parse_shift_page(page: Dict[str, Any]) -> Dict[str, Any]:
    properties = page.get("properties", {})

    name = ""
    for prop in properties.values():
        if prop.get("type") == "title":
            name = "".join(p.get("plain_text", "") for p in prop.get("title", []))
            break

    entries_raw = ""
    for key in ("entries", "Entries"):
        prop = properties.get(key)
        if prop and prop.get("type") == "rich_text":
            entries_raw = "".join(
                p.get("plain_text", "") for p in prop.get("rich_text", [])
            )
            break

    shift_start: Optional[str] = None
    for key in ("shift_start", "Shift_start"):
        prop = properties.get(key)
        if prop and prop.get("type") == "date":
            date_obj = prop.get("date")
            if date_obj:
                shift_start = date_obj.get("start")
            break

    entries = parse_entries(entries_raw)

    return {
        "id": page.get("id", ""),
        "name": name,
        "url": page.get("url", ""),
        "shift_start": shift_start,
        "entries": entries,
        "entries_raw": entries_raw,
        "is_open": is_shift_open(entries),
    }
