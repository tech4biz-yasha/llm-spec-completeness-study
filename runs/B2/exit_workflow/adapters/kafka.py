"""Kafka producer adapter (AGENTS.md stack: "Kafka producer for events").

The producer object is injected so the module does not own broker configuration
or its lifecycle. Any exception raised here is treated by NotificationService as
a dispatch failure and drives the EXIT-04 retry/dead-letter path.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from ..ports import OutboundEvent


class KafkaProducerLike(Protocol):
    """The subset of aiokafka.AIOKafkaProducer this module uses."""

    async def send_and_wait(
        self, topic: str, value: bytes, key: bytes | None = ..., headers: Any = ...
    ) -> Any: ...


class KafkaEventPublisher:
    def __init__(self, producer: KafkaProducerLike) -> None:
        self._producer = producer

    async def publish(self, event: OutboundEvent) -> None:
        await self._producer.send_and_wait(
            event.topic,
            value=json.dumps(event.payload, separators=(",", ":")).encode("utf-8"),
            key=event.key.encode("utf-8"),
            headers=[("event_type", event.event_type.encode("utf-8"))],
        )


class InMemoryEventPublisher:
    """Collects events instead of publishing. For local runs and tests."""

    def __init__(self) -> None:
        self.published: list[OutboundEvent] = []
        self.fail_next: int = 0

    async def publish(self, event: OutboundEvent) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("kafka unavailable")
        self.published.append(event)
