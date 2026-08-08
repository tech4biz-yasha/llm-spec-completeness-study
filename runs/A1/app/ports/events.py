"""Domain event publication (SRS §7 puts Kafka on the event path)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.models.audit import OutboxEvent

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    """Event names are part of the module's public contract; do not rename in place."""

    WORKFLOW_INITIATED = "exit.workflow.initiated"
    WORKFLOW_SUBMITTED = "exit.workflow.submitted"
    WORKFLOW_APPROVED = "exit.workflow.approved"
    WORKFLOW_REJECTED = "exit.workflow.rejected"
    INSPECTION_REQUESTED = "exit.inspection.requested"
    INSPECTION_SLOTS_PROPOSED = "exit.inspection.slots_proposed"
    INSPECTION_SCHEDULED = "exit.inspection.scheduled"
    INSPECTION_COMPLETED = "exit.inspection.completed"
    DAMAGE_REPORT_SUBMITTED = "exit.damage_report.submitted"
    SETTLEMENT_COMPUTED = "exit.settlement.computed"
    SETTLEMENT_APPROVED = "exit.settlement.approved"
    SETTLEMENT_CLOSED = "exit.settlement.closed"
    PAYMENT_SUCCEEDED = "exit.payment.succeeded"
    NOC_ISSUED = "exit.noc.issued"
    WORKFLOW_COMPLETED = "exit.workflow.completed"
    WORKFLOW_CANCELLED = "exit.workflow.cancelled"
    #: Notification intents ride the same outbox so there is a single delivery mechanism.
    NOTIFICATION_REQUESTED = "exit.notification.requested"


@runtime_checkable
class EventPublisher(Protocol):
    async def publish(self, events: Sequence[OutboxEvent]) -> None:
        """Publish a batch. Must raise on failure so the relay can retry."""

    async def close(self) -> None: ...


class NullEventPublisher:
    """Used when ``KAFKA_ENABLED=false``. Drains the outbox and logs, so local and test
    environments exercise the same relay path as production."""

    async def publish(self, events: Sequence[OutboxEvent]) -> None:
        for event in events:
            logger.info(
                "event published (null publisher)",
                extra={
                    "event_type": event.event_type,
                    "topic": event.topic,
                    "aggregate_id": str(event.aggregate_id),
                },
            )

    async def close(self) -> None:
        return None


class KafkaEventPublisher:
    """Publishes to Kafka via ``aiokafka``, imported lazily so the dependency is optional."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._producer: object | None = None

    async def _get_producer(self) -> object:
        if self._producer is None:
            try:
                from aiokafka import AIOKafkaProducer  # type: ignore[import-not-found]
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "KAFKA_ENABLED=true requires the 'aiokafka' package to be installed"
                ) from exc
            producer = AIOKafkaProducer(bootstrap_servers=self._bootstrap_servers)
            await producer.start()
            self._producer = producer
        return self._producer

    async def publish(self, events: Sequence[OutboxEvent]) -> None:  # pragma: no cover - I/O
        import json

        producer = await self._get_producer()
        for event in events:
            await producer.send_and_wait(  # type: ignore[attr-defined]
                event.topic,
                key=event.partition_key.encode(),
                value=json.dumps(event.payload, default=str).encode(),
                headers=[(k, str(v).encode()) for k, v in (event.headers or {}).items()],
            )

    async def close(self) -> None:  # pragma: no cover - I/O
        if self._producer is not None:
            await self._producer.stop()  # type: ignore[attr-defined]
            self._producer = None
