"""Domain-event recording into the transactional outbox."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.domain.events import AGGREGATE_TYPE, SCHEMA_VERSION, DomainEvent
from app.models.outbox import OutboxMessage
from app.repositories.support import OutboxRepository


class EventRecorder:
    """Writes events to the outbox in the caller's transaction.

    Nothing here touches the network. Relay is the dispatcher's job
    (:mod:`app.workers.outbox_dispatcher`).
    """

    def __init__(self, session: AsyncSession, settings: Settings, clock: Clock) -> None:
        self._repo = OutboxRepository(session)
        self._settings = settings
        self._clock = clock

    def record(
        self,
        event: DomainEvent,
        *,
        ctx: RequestContext | None = None,
        topic: str | None = None,
    ) -> OutboxMessage:
        now = self._clock.now()
        headers: dict[str, Any] = {
            "content-type": "application/json",
            "schema-version": str(SCHEMA_VERSION),
            "aggregate-type": AGGREGATE_TYPE,
            "event-type": event.event_type,
        }
        if ctx is not None:
            if ctx.request_id:
                headers["request-id"] = ctx.request_id
            headers["actor-role"] = ctx.principal.role.value
            headers["actor-id"] = str(ctx.principal.actor_id)

        message = OutboxMessage(
            event_id=uuid.uuid4(),
            aggregate_type=AGGREGATE_TYPE,
            aggregate_id=event.workflow_id,
            event_type=event.event_type,
            partition_key=event.key(),
            topic=topic or self._settings.kafka_topic,
            payload=event.envelope(occurred_at=now),
            headers=headers,
            occurred_at=event.occurred_at or now,
            available_at=now,
        )
        self._repo.add(message)
        return message

    def record_all(
        self, events: list[DomainEvent], *, ctx: RequestContext | None = None
    ) -> list[OutboxMessage]:
        return [self.record(e, ctx=ctx) for e in events]
