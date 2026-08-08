"""Notifiers.

The SRS names email as the channel for owner and inspection-agency contact but no
provider is specified, so the module ships a structured-log notifier (the default, and
what CI runs against) and a webhook notifier that hands the payload to whatever
notification service the platform already operates.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.core.logging import get_logger
from app.ports.notifications import Notification, Notifier

log = get_logger(__name__)


def _serialise(notification: Notification) -> dict[str, Any]:
    return {
        "template": notification.template.value,
        "channels": [c.value for c in notification.channels],
        "recipients": [
            {"actor_id": r.actor_id, "email": r.email, "phone": r.phone, "name": r.name}
            for r in notification.recipients
        ],
        "context": notification.context,
        "dedupe_key": notification.dedupe_key,
    }


class LoggingNotifier(Notifier):
    """Emits the notification as a structured log line. Never raises."""

    async def send(self, notification: Notification) -> None:
        log.info("notification.dispatch", **_serialise(notification))


class WebhookNotifier(Notifier):
    """POSTs the notification to the platform's notification service."""

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._client = client
        self._timeout = timeout

    async def send(self, notification: Notification) -> None:
        payload = _serialise(notification)
        headers = {"Content-Type": "application/json"}
        if notification.dedupe_key:
            headers["Idempotency-Key"] = notification.dedupe_key
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.post(self._url, json=payload, headers=headers)
            response.raise_for_status()
        finally:
            if self._client is None:
                await client.aclose()


class NullNotifier(Notifier):
    """Discards notifications; used by tests that assert on events instead."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)
