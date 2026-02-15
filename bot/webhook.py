import asyncio
import logging
import os
from typing import Any, Dict, Optional

import httpx

VERIFY_SSL = os.getenv("WEBHOOK_VERIFY_SSL", "true").lower() in ("1", "true", "yes", "y")
FOLLOW_REDIRECTS = os.getenv("WEBHOOK_FOLLOW_REDIRECTS", "true").lower() in ("1", "true", "yes", "y")


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

        timeout = httpx.Timeout(self._timeout_seconds)

        for attempt in range(1, self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    verify=VERIFY_SSL,
                    follow_redirects=FOLLOW_REDIRECTS,
                ) as client:
                    resp = await client.post(self._webhook_url, json=payload, headers=headers)

                # Se não seguir redirects, 308 cai aqui; se seguir, pode cair em 4xx/5xx.
                if 200 <= resp.status_code < 300:
                    self._logger.info(
                        "Webhook event sent successfully",
                        extra={"context": {"attempt": attempt, "url": self._webhook_url, "status": resp.status_code}},
                    )
                    return True

                # Trata status ruim como erro (logando body curto)
                body_preview = (resp.text or "")[:300]
                raise httpx.HTTPStatusError(
                    f"Non-2xx response: {resp.status_code}. Body: {body_preview}",
                    request=resp.request,
                    response=resp,
                )

            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                self._logger.warning(
                    "Webhook delivery failed",
                    extra={
                        "context": {
                            "attempt": attempt,
                            "max_retries": self._max_retries,
                            "url": self._webhook_url,
                            "verify_ssl": VERIFY_SSL,
                            "follow_redirects": FOLLOW_REDIRECTS,
                            "error": str(exc),
                        }
                    },
                )

                if attempt < self._max_retries:
                    await asyncio.sleep(attempt)  # backoff simples

        self._logger.error(
            "Webhook delivery exhausted retries",
            extra={"context": {"max_retries": self._max_retries, "url": self._webhook_url}},
        )
        return False
