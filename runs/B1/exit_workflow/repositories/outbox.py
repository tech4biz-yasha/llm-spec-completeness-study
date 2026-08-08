"""Transactional outbox access (rules.yaml#EXIT-04)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import EventOutbox
from exit_workflow.domain.enums import EventType, OutboxStatus


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def enqueue(
        self,
        *,
        topic: str,
        event_type: EventType,
        event_key: str,
        payload: dict[str, Any],
        available_at: datetime,
    ) -> EventOutbox:
        """Queue an event inside the caller's transaction.

        The row commits with the workflow; dispatch happens afterwards
        (rules.yaml#EXIT-04: "emitted AFTER the initiation transaction commits").
        """
        row = EventOutbox(
            id=uuid.uuid4(),
            topic=topic,
            event_type=str(event_type),
            event_key=event_key,
            payload=payload,
            status=str(OutboxStatus.PENDING),
            attempts=0,
            next_attempt_at=available_at,
        )
        self._session.add(row)
        return row

    async def get(self, event_id: uuid.UUID, *, for_update: bool = False) -> EventOutbox | None:
        statement = select(EventOutbox).where(EventOutbox.id == event_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def claim_due(self, *, now: datetime, limit: int = 100) -> list[EventOutbox]:
        """Lock the events whose backoff has elapsed.

        ``skip_locked`` lets several dispatcher workers run without one blocking
        another or double-publishing a row.
        """
        result = await self._session.scalars(
            select(EventOutbox)
            .where(
                EventOutbox.status == str(OutboxStatus.PENDING),
                EventOutbox.next_attempt_at <= now,
            )
            .order_by(EventOutbox.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result)

    async def pending_count(self) -> int:
        result = await self._session.scalars(
            select(EventOutbox.id).where(EventOutbox.status == str(OutboxStatus.PENDING))
        )
        return len(list(result))
