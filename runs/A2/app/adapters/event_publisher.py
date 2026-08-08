"""Event publisher adapters."""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger
from app.ports.event_publisher import EventPublisher, EventPublishError, OutgoingEvent

log = get_logger(__name__)


class LoggingEventPublisher(EventPublisher):
    """Writes events to the structured log. Default outside production."""

    async def publish(self, event: OutgoingEvent) -> None:
        log.info(
            "event.publish",
            topic=event.topic,
            key=event.key,
            event_type=event.value.get("event_type"),
        )

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class CollectingEventPublisher(EventPublisher):
    """Captures events in memory so tests can assert on the emitted stream."""

    def __init__(self) -> None:
        self.events: list[OutgoingEvent] = []

    async def publish(self, event: OutgoingEvent) -> None:
        self.events.append(event)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class KafkaEventPublisher(EventPublisher):
    """Publishes to Kafka (SRS §7). Requires the ``kafka`` extra."""

    def __init__(self, bootstrap_servers: str, *, client_id: str = "exit-workflow") -> None:
        self._bootstrap_servers = bootstrap_servers
        self._client_id = client_id
        self._producer: Any | None = None

    async def start(self) -> None:
        if self._producer is not None:
            return
        try:
            from aiokafka import AIOKafkaProducer  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Kafka publishing requires the 'kafka' extra: "
                "pip install 'meridian-exit-workflow[kafka]'"
            ) from exc
        import json  # noqa: PLC0415

        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            client_id=self._client_id,
            # Durability over throughput: these events drive money and legal documents.
            acks="all",
            enable_idempotence=True,
            value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode(),
            key_serializer=lambda k: k.encode(),
        )
        await self._producer.start()

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, event: OutgoingEvent) -> None:
        if self._producer is None:
            await self.start()
        assert self._producer is not None
        try:
            await self._producer.send_and_wait(
                event.topic,
                value=event.value,
                key=event.key,
                headers=[(k, v.encode()) for k, v in event.headers.items()],
            )
        except Exception as exc:  # noqa: BLE001 - aiokafka raises a wide family
            raise EventPublishError(str(exc)) from exc
