import logging
from typing import Any, Dict, List, Optional

import httpx

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionClient:
    def __init__(
        self,
        token: str,
        database_id: str,
        shifts_database_id: Optional[str] = None,
    ) -> None:
        self._logger = logging.getLogger(__name__)
        self._token = token
        self._database_id = database_id
        self._shifts_database_id = shifts_database_id
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    async def fetch_status_options(self) -> List[str]:
        url = f"{NOTION_API_BASE}/databases/{self._database_id}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(url, headers=self._headers)
            response.raise_for_status()
            data = response.json()

        properties = data.get("properties", {})
        for key in ("Status", "status"):
            prop = properties.get(key)
            if not prop:
                continue
            ptype = prop.get("type")
            if ptype == "status":
                groups = prop.get("status", {}).get("options", [])
                return [opt["name"] for opt in groups if "name" in opt]
            if ptype == "select":
                options = prop.get("select", {}).get("options", [])
                return [opt["name"] for opt in options if "name" in opt]

        return ["Not started", "In progress", "Done"]

    async def create_task(
        self,
        name: str,
        status: str,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        url = f"{NOTION_API_BASE}/pages"

        properties: Dict[str, Any] = {
            "Name": {"title": [{"text": {"content": name}}]},
            "status": {"status": {"name": status}},
        }
        if description:
            properties["description"] = {
                "rich_text": [{"text": {"content": description}}]
            }

        payload = {
            "parent": {"database_id": self._database_id},
            "properties": properties,
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                response = await client.post(url, headers=self._headers, json=payload)
                response.raise_for_status()
                page = response.json()
        except httpx.HTTPStatusError as exc:
            self._logger.error(
                "Notion API error creating task",
                extra={
                    "context": {
                        "status": exc.response.status_code,
                        "body": exc.response.text[:500],
                    }
                },
            )
            raise

        return self._parse_page(page)

    async def _fetch_page_time_min(self, page_id: str) -> int:
        url = f"{NOTION_API_BASE}/pages/{page_id}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(url, headers=self._headers)
            response.raise_for_status()
            page = response.json()

        properties = page.get("properties", {})
        for key in ("time_min", "Time_min", "time"):
            prop = properties.get(key)
            if prop and prop.get("type") == "number":
                return prop.get("number") or 0
        return 0

    async def update_task(
        self,
        page_id: str,
        time_min: Optional[int] = None,
        status: Optional[str] = None,
    ) -> int:
        """Returns the new total time_min after summing."""
        url = f"{NOTION_API_BASE}/pages/{page_id}"

        properties: Dict[str, Any] = {}
        total_time = 0

        if time_min is not None:
            current = await self._fetch_page_time_min(page_id)
            total_time = current + time_min
            properties["time_min"] = {"number": total_time}

        if status is not None:
            properties["status"] = {"status": {"name": status}}

        if not properties:
            return total_time

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                response = await client.patch(
                    url, headers=self._headers, json={"properties": properties}
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            self._logger.error(
                "Notion API error updating task",
                extra={
                    "context": {
                        "page_id": page_id,
                        "status": exc.response.status_code,
                        "body": exc.response.text[:500],
                    }
                },
            )
            raise

        return total_time

    async def fetch_tasks(self) -> List[Dict[str, Any]]:
        url = f"{NOTION_API_BASE}/databases/{self._database_id}/query"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                response = await client.post(url, headers=self._headers, json={})
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            self._logger.error(
                "Notion API returned error",
                extra={
                    "context": {
                        "status": exc.response.status_code,
                        "body": exc.response.text[:500],
                    }
                },
            )
            raise
        except httpx.HTTPError as exc:
            self._logger.error(
                "Failed to reach Notion API",
                extra={"context": {"error": str(exc)}},
            )
            raise

        return [self._parse_page(page) for page in data.get("results", [])]

    # ------------------------------------------------------------------
    # Shift methods
    # ------------------------------------------------------------------

    async def create_shift(self, name: str, shift_start: str, entries_json: str) -> Dict[str, Any]:
        if not self._shifts_database_id:
            raise RuntimeError("Shifts database not configured")

        url = f"{NOTION_API_BASE}/pages"
        payload = {
            "parent": {"database_id": self._shifts_database_id},
            "properties": {
                "Name": {"title": [{"text": {"content": name}}]},
                "shift_start": {"date": {"start": shift_start}},
                "entries": {"rich_text": [{"text": {"content": entries_json}}]},
            },
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(url, headers=self._headers, json=payload)
            response.raise_for_status()
            return response.json()

    async def update_shift_entries(self, page_id: str, entries_json: str) -> None:
        url = f"{NOTION_API_BASE}/pages/{page_id}"
        payload = {
            "properties": {
                "entries": {"rich_text": [{"text": {"content": entries_json}}]},
            }
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.patch(url, headers=self._headers, json=payload)
            response.raise_for_status()

    async def fetch_shifts(self, limit: int = 10) -> List[Dict[str, Any]]:
        if not self._shifts_database_id:
            raise RuntimeError("Shifts database not configured")

        url = f"{NOTION_API_BASE}/databases/{self._shifts_database_id}/query"
        body: Dict[str, Any] = {
            "sorts": [{"property": "shift_start", "direction": "descending"}],
            "page_size": limit,
        }

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(url, headers=self._headers, json=body)
            response.raise_for_status()
            data = response.json()

        return data.get("results", [])

    async def delete_shift(self, page_id: str) -> None:
        url = f"{NOTION_API_BASE}/pages/{page_id}"
        payload = {"archived": True}

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.patch(url, headers=self._headers, json=payload)
            response.raise_for_status()

    def _parse_page(self, page: Dict[str, Any]) -> Dict[str, Any]:
        properties = page.get("properties", {})
        name = self._extract_title(properties)

        return {
            "id": page.get("id", ""),
            "name": name,
            "url": page.get("url", ""),
            "property_due": self._extract_date(properties),
            "property_description": self._extract_rich_text(properties, "Description"),
            "property_status": self._extract_status(properties),
            "property_name": name,
        }

    def _extract_title(self, properties: Dict[str, Any]) -> str:
        for prop in properties.values():
            if prop.get("type") == "title":
                return "".join(
                    p.get("plain_text", "") for p in prop.get("title", [])
                )
        return ""

    def _extract_status(self, properties: Dict[str, Any]) -> str:
        for key in ("Status", "status"):
            prop = properties.get(key)
            if not prop:
                continue
            ptype = prop.get("type")
            if ptype == "status":
                status = prop.get("status")
                if status:
                    return status.get("name", "")
            elif ptype == "select":
                select = prop.get("select")
                if select:
                    return select.get("name", "")
        return ""

    def _extract_date(self, properties: Dict[str, Any]) -> Optional[str]:
        for key in ("Due", "due", "Date", "date", "Due Date", "due date"):
            prop = properties.get(key)
            if prop and prop.get("type") == "date":
                date_obj = prop.get("date")
                if date_obj:
                    return date_obj.get("start")
        return None

    def _extract_rich_text(self, properties: Dict[str, Any], *keys: str) -> str:
        search_keys = list(keys) + [k.lower() for k in keys]
        for key in search_keys:
            prop = properties.get(key)
            if prop and prop.get("type") == "rich_text":
                return "".join(
                    p.get("plain_text", "") for p in prop.get("rich_text", [])
                )
        return ""
