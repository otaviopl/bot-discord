from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


@dataclass
class TimerEntry:
    task_id: str
    task_name: str
    task_url: str
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def elapsed_minutes(self) -> int:
        delta = datetime.now(timezone.utc) - self.started_at
        return int(delta.total_seconds() / 60)

    @property
    def elapsed_display(self) -> str:
        mins = self.elapsed_minutes
        if mins < 60:
            return f"{mins}min"
        hours, remainder = divmod(mins, 60)
        return f"{hours}h {remainder}min"


class TimerManager:
    def __init__(self) -> None:
        self._timers: Dict[int, List[TimerEntry]] = {}

    def start(self, user_id: int, task_id: str, task_name: str, task_url: str) -> TimerEntry:
        entry = TimerEntry(task_id=task_id, task_name=task_name, task_url=task_url)
        self._timers.setdefault(user_id, []).append(entry)
        return entry

    def get_active(self, user_id: int) -> List[TimerEntry]:
        return list(self._timers.get(user_id, []))

    def stop(self, user_id: int, task_id: str) -> Optional[TimerEntry]:
        entries = self._timers.get(user_id, [])
        for i, entry in enumerate(entries):
            if entry.task_id == task_id:
                return entries.pop(i)
        return None
