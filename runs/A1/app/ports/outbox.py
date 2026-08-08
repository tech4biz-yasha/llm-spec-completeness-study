"""Transactional outbox: record in the state transaction, deliver afterwards.

Every outbound side effect the module has — Kafka domain events and email/SMS notifications —
is written as an ``outbox_events`` row inside the same transaction as the state change that
caused it. If the transaction rolls back, the side effect never happened. The relay then
delivers rows at least once, so a consumer must dedupe on ``event_id``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import OutboxEvent
from app.models.base import utcnow
from app.ports.events import EventPublisher, EventType
from app.ports.notifications import Notification, NotificationPort

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 10


class OutboxRecorder:
    """Appends events to the outbox. Never commits — the caller's unit of work does."""

    def __init__(self, session: AsyncSession, *, topic_prefix: str) -> None:
        self._session = session
        self._topic_prefix = topic_prefix

    def _topic(self, event_type: EventType) -> str:
        # "exit.workflow.initiated" -> "<prefix>.workflow"
        parts = event_type.value.split(".")
        stream = parts[1] if len(parts) > 2 else parts[-1]
        return f"{self._topic_prefix}.{stream}"

    def record(
        self,
        event_type: EventType,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        partition_key: str,
        payload: dict[str, Any],
        headers: dict[str, Any] | None = None,
    ) -> OutboxEvent:
        event = OutboxEvent(
            topic=self._topic(event_type),
            event_type=event_type.value,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            partition_key=partition_key,
            payload={
                "event_id": str(uuid.uuid4()),
                "event_type": event_type.value,
                "occurred_at": utcnow().isoformat(),
                **payload,
            },
            headers=headers or {},
        )
        self._session.add(event)
        return event

    def record_notification(
        self,
        notification: Notification,
        *,
        aggregate_id: uuid.UUID,
        partition_key: str,
    ) -> OutboxEvent:
        return self.record(
            EventType.NOTIFICATION_REQUESTED,
            aggregate_type="Notification",
            aggregate_id=aggregate_id,
            partition_key=partition_key,
            payload={"notification": notification.as_payload()},
        )


class OutboxRelay:
    """Drains unpublished outbox rows and dispatches them to the right adapter.

    Rows are claimed with ``FOR UPDATE SKIP LOCKED`` so several replicas can relay
    concurrently without double-delivering the same row.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        publisher: EventPublisher,
        notifier: NotificationPort,
        batch_size: int = 100,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._publisher = publisher
        self._notifier = notifier
        self._batch_size = batch_size

    async def drain_once(self) -> int:
        """Process one batch. Returns the number of rows successfully published."""
        published = 0
        async with self._sessionmaker() as session:
            async with session.begin():
                rows = (
                    (
                        await session.execute(
                            sa.select(OutboxEvent)
                            .where(
                                OutboxEvent.published_at.is_(None),
                                OutboxEvent.attempts < MAX_ATTEMPTS,
                            )
                            .order_by(OutboxEvent.id)
                            .limit(self._batch_size)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    try:
                        await self._dispatch(row)
                    except Exception as exc:  # noqa: BLE001 - relay must not die on one row
                        row.attempts += 1
                        row.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                        logger.warning(
                            "outbox delivery failed",
                            extra={
                                "outbox_id": row.id,
                                "event_type": row.event_type,
                                "attempts": row.attempts,
                            },
                            exc_info=exc,
                        )
                    else:
                        row.published_at = utcnow()
                        published += 1
        return published

    async def _dispatch(self, row: OutboxEvent) -> None:
        if row.event_type == EventType.NOTIFICATION_REQUESTED.value:
            payload = row.payload.get("notification") or {}
            await self._notifier.send(_notification_from_payload(payload))
        else:
            await self._publisher.publish([row])

    async def drain_all(self, *, max_batches: int = 100) -> int:
        total = 0
        for _ in range(max_batches):
            count = await self.drain_once()
            total += count
            if count < self._batch_size:
                break
        return total


def _notification_from_payload(payload: dict[str, Any]) -> Notification:
    from app.ports.notifications import Channel, NotificationTemplate

    return Notification(
        template=NotificationTemplate(payload["template"]),
        channel=Channel(payload["channel"]),
        recipient=payload["recipient"],
        context=payload.get("context") or {},
        subject=payload.get("subject"),
    )


def pending_count_stmt() -> sa.Select[tuple[int]]:
    return sa.select(sa.func.count()).select_from(OutboxEvent).where(
        OutboxEvent.published_at.is_(None)
    )


__all__ = ["MAX_ATTEMPTS", "OutboxRecorder", "OutboxRelay", "pending_count_stmt"]
