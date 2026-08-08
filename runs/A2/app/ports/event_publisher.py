"""Event bus port (Kafka in production, per SRS §7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class OutgoingEvent:
    topic: str
    key: str
    value: dict[str, Any]
    headers: dict[str, str]


class EventPublisher(Protocol):
    async def publish(self, event: OutgoingEvent) -> None:
        """Publish one event. Raise to have the outbox retry with backoff."""
        ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class EventPublishError(RuntimeError):
    """Transient publication failure; the outbox will retry."""
