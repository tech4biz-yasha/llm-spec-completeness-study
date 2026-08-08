"""Domain events: transactional outbox writer plus Kafka/logging publishers."""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.logging import get_logger
from exit_workflow.core.serialization import jsonable
from exit_workflow.models.outbox import OutboxEvent
from exit_workflow.services.context import ServiceContext

log = get_logger(__name__)


class EventType:
    """Contract with downstream consumers — treat these strings as public."""

    WORKFLOW_INITIATED = "exit_workflow.initiated"
    WORKFLOW_SUBMITTED = "exit_workflow.submitted"
    WORKFLOW_APPROVED = "exit_workflow.approved"
    WORKFLOW_REJECTED = "exit_workflow.rejected"
    WORKFLOW_CANCELLED = "exit_workflow.cancelled"
    WORKFLOW_STATUS_CHANGED = "exit_workflow.status_changed"
    WORKFLOW_COMPLETED = "exit_workflow.completed"
    #: BR-1 signals for the contract service.
    LOCK_ACQUIRED = "exit_workflow.lock_acquired"
    LOCK_RELEASED = "exit_workflow.lock_released"

    DOCUMENT_UPLOADED = "exit_document.uploaded"

    INSPECTION_REQUESTED = "inspection.requested"
    INSPECTION_SLOTS_PROPOSED = "inspection.slots_proposed"
    INSPECTION_SCHEDULED = "inspection.scheduled"
    INSPECTION_COMPLETED = "inspection.completed"
    INSPECTION_CANCELLED = "inspection.cancelled"

    DAMAGE_REPORT_SUBMITTED = "damage_report.submitted"
    DAMAGE_REPORT_DISPUTED = "damage_report.disputed"
    DAMAGE_REPORT_DISPUTE_RESOLVED = "damage_report.dispute_resolved"
    DAMAGE_REPORT_FINALIZED = "damage_report.finalized"

    SETTLEMENT_COMPUTED = "settlement.computed"
    SETTLEMENT_PAID = "settlement.paid"
    SETTLEMENT_PAYMENT_FAILED = "settlement.payment_failed"

    NOC_ISSUED = "noc.issued"
    NOC_DOWNLOADED = "noc.downloaded"


class AggregateType:
    WORKFLOW = "exit_workflow"
    INSPECTION = "inspection"
    DAMAGE_REPORT = "damage_report"
    SETTLEMENT = "settlement"
    NOC = "exit_noc"
    DOCUMENT = "exit_document"


class EventRecorder:
    """Writes outbox rows inside the caller's transaction."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def emit(
        self,
        ctx: ServiceContext,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        workflow_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> OutboxEvent:
        now = utcnow()
        event = OutboxEvent(
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            # Keyed by workflow so every event for one exit is strictly ordered
            # on its Kafka partition.
            partition_key=str(workflow_id),
            event_type=event_type,
            payload=jsonable(
                {
                    "event_type": event_type,
                    "workflow_id": str(workflow_id),
                    "aggregate_id": str(aggregate_id),
                    "occurred_at": now,
                    "actor": {
                        "type": ctx.actor_type.value,
                        "id": str(ctx.actor_id) if ctx.actor_id else None,
                    },
                    "data": payload,
                }
            ),
            headers=jsonable(
                {
                    "content-type": "application/json",
                    "x-request-id": ctx.request_id,
                    "x-event-type": event_type,
                }
            ),
            occurred_at=now,
            available_at=now,
        )
        self._session.add(event)
        return event


class EventPublisher(Protocol):
    async def publish(
        self, topic: str, key: str, value: bytes, headers: dict[str, str]
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class LoggingEventPublisher:
    """Default publisher: structured log line per event.

    Used when Kafka is not configured. Every event still lands in the outbox
    table, so switching to Kafka later loses nothing.
    """

    async def publish(self, topic: str, key: str, value: bytes, headers: dict[str, str]) -> None:
        log.info(
            "domain_event_published",
            topic=topic,
            key=key,
            event=json.loads(value.decode("utf-8")),
        )

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class KafkaEventPublisher:
    """aiokafka adapter (install the ``kafka`` extra)."""

    def __init__(self, bootstrap_servers: str) -> None:
        self._bootstrap_servers = bootstrap_servers
        self._producer: Any = None

    async def start(self) -> None:
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "EXITWF_KAFKA_ENABLED=true requires the 'kafka' extra (aiokafka)."
            ) from exc
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap_servers,
            enable_idempotence=True,
            acks="all",
        )
        await self._producer.start()

    async def stop(self) -> None:  # pragma: no cover - requires a broker
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(
        self, topic: str, key: str, value: bytes, headers: dict[str, str]
    ) -> None:  # pragma: no cover - requires a broker
        if self._producer is None:
            raise RuntimeError("Kafka publisher was not started.")
        await self._producer.send_and_wait(
            topic,
            value=value,
            key=key.encode("utf-8"),
            headers=[(k, str(v).encode("utf-8")) for k, v in headers.items() if v is not None],
        )


def build_publisher(settings: Settings) -> EventPublisher:
    if settings.kafka_enabled:
        return KafkaEventPublisher(settings.kafka_bootstrap_servers)
    return LoggingEventPublisher()


def topic_for(settings: Settings, aggregate_type: str) -> str:
    return f"{settings.kafka_topic_prefix}.{aggregate_type}"
