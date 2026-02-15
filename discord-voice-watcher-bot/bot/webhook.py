import asyncio
import logging
from typing import Any, Dict, Optional

import httpx


class WebhookDispatcher:
    def __init__(
        self,
        webhook_url: str,
        webhook_secret: Optional[str] = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self._logger = logging.getLogger(__name__)
        self._webhook_url = webhook_url
        self._webhook_secret = webhook_secret
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries

    async def send_event(self, payload: Dict[str, Any]) -> bool:
        headers = {"Content-Type": "application/json"}
        if self._webhook_secret:
            headers["X-Discord-Webhook-Secret"] = self._webhook_secret

        for attempt in range(1, self._max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                    response = await client.post(
                        self._webhook_url, json=payload, headers=headers
                    )
                    response.raise_for_status()

                self._logger.info(
                    "Webhook event sent successfully",
                    extra={"context": {"attempt": attempt, "url": self._webhook_url}},
                )
                return True

            except httpx.HTTPError as exc:
                self._logger.warning(
                    "Webhook delivery failed",
                    extra={
                        "context": {
                            "attempt": attempt,
                            "max_retries": self._max_retries,
                            "url": self._webhook_url,
                            "error": str(exc),
                        }
                    },
                )

                if attempt < self._max_retries:
                    # Small linear backoff to protect external service and avoid immediate retry storms.
                    await asyncio.sleep(attempt)

        self._logger.error(
            "Webhook delivery exhausted retries",
            extra={"context": {"max_retries": self._max_retries, "url": self._webhook_url}},
        )
        return False

