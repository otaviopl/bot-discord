import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from googleapiclient.discovery import build

from .calendar_auth import CalendarAuth


class CalendarClient:
    def __init__(self, auth: CalendarAuth, calendar_id: str = "primary", timezone: str = "America/Sao_Paulo") -> None:
        self._auth = auth
        self._calendar_id = calendar_id
        self._timezone = timezone
        self._logger = logging.getLogger(__name__)

    async def list_events(self, days: int = 7) -> List[Dict[str, Any]]:
        now = datetime.now(tz=__import__("datetime").timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days)).isoformat()

        def _fetch() -> List[Dict[str, Any]]:
            service = self._build_service()
            result = (
                service.events()
                .list(
                    calendarId=self._calendar_id,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=10,
                )
                .execute()
            )
            return result.get("items", [])

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch)

    async def create_event(
        self,
        title: str,
        start_dt: datetime,
        duration_minutes: int = 60,
        description: str = "",
    ) -> Dict[str, Any]:
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        event_body = {
            "summary": title,
            "description": description,
            "start": {
                "dateTime": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": self._timezone,
            },
            "end": {
                "dateTime": end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": self._timezone,
            },
        }

        def _create() -> Dict[str, Any]:
            service = self._build_service()
            return service.events().insert(calendarId=self._calendar_id, body=event_body).execute()

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _create)

    async def delete_event(self, event_id: str) -> None:
        def _delete() -> None:
            service = self._build_service()
            service.events().delete(calendarId=self._calendar_id, eventId=event_id).execute()

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _delete)

    def _build_service(self):
        creds = self._auth.get_credentials()
        if creds is None:
            raise RuntimeError("Not authenticated with Google Calendar")
        return build("calendar", "v3", credentials=creds)
