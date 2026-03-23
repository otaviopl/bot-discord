import logging
from typing import Any, Dict, List, Optional

import httpx

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


class NotionClient:
    def __init__(self, token: str, database_id: str) -> None:
        self._logger = logging.getLogger(__name__)
        self._token = token
        self._database_id = database_id
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

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
