"""Transactional outbox for the owner notification.

rules.yaml#EXIT-04: "Owner notification is emitted AFTER the initiation transaction
commits. If dispatch fails, the workflow does NOT roll back; the event queues for retry
with exponential backoff, 5 attempts, then dead-letter + admin alert."

The row is written inside the initiation transaction — that is what makes the event
durable and exactly-once-enqueued — while the publish call happens strictly after that
transaction commits. edges.yaml#X-002 is satisfied structurally: a publish failure cannot
roll anything back, because by then there is nothing open to roll back.

The backoff *base* is not specified by the kit and is a deployment setting
(``EXIT_NOTIFICATION_BACKOFF_BASE_SECONDS``). See blockers.md#B-10.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import UTC
from ..config import Settings
from ..db.models import OutboxEvent
from ..enums import AdminTaskType, OutboxStatus
from ..ports.events import Event, EventPublisher
from .admin import open_admin_task
from .ids import new_event_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    sent: int
    retried: int
    dead_lettered: int


def enqueue(
    session: Session,
    *,
    topic: str,
    key: str,
    payload: dict[str, Any],
    workflow_id: str,
    occurred_at: datetime,
) -> OutboxEvent:
    """Write the event inside the caller's transaction. No I/O here."""
    event = OutboxEvent(
        id=new_event_id(),
        topic=topic,
        event_key=key,
        payload=payload,
        status=str(OutboxStatus.PENDING),
        attempts=0,
        next_attempt_at=occurred_at.astimezone(UTC),
        workflow_id=workflow_id,
        created_at=occurred_at.astimezone(UTC),
    )
    session.add(event)
    return event


def backoff_delay(attempts: int, settings: Settings) -> timedelta:
    """Exponential backoff. rules.yaml#EXIT-04."""
    seconds = settings.notification_backoff_base_seconds * (2 ** max(attempts - 1, 0))
    return timedelta(seconds=min(seconds, settings.notification_backoff_cap_seconds))


def _attempt(
    session: Session,
    event: OutboxEvent,
    *,
    publisher: EventPublisher,
    settings: Settings,
    now: datetime,
) -> str:
    """Publish one event. Returns 'sent', 'retried' or 'dead_lettered'."""
    event.attempts += 1
    try:
        publisher.publish(Event(topic=event.topic, key=event.event_key, payload=event.payload))
    except Exception as exc:  # any publisher failure retries; PublishError is the usual one
        event.last_error = f"{type(exc).__name__}: {exc}"
        if event.attempts >= settings.notification_max_attempts:
            # rules.yaml#EXIT-04 — dead-letter plus admin alert after 5 attempts.
            event.status = str(OutboxStatus.DEAD_LETTER)
            open_admin_task(
                session,
                task_type=AdminTaskType.OWNER_NOTIFICATION_DEAD_LETTER,
                workflow_id=event.workflow_id or "",
                payload={
                    "event_id": event.id,
                    "topic": event.topic,
                    "attempts": event.attempts,
                    "last_error": event.last_error,
                },
                occurred_at=now,
            )
            logger.error(
                "owner notification dead-lettered event_id=%s workflow_id=%s attempts=%s",
                event.id,
                event.workflow_id,
                event.attempts,
            )
            return "dead_lettered"
        event.next_attempt_at = now.astimezone(UTC) + backoff_delay(event.attempts, settings)
        logger.warning(
            "owner notification dispatch failed event_id=%s attempt=%s retry_at=%s",
            event.id,
            event.attempts,
            event.next_attempt_at,
        )
        return "retried"

    event.status = str(OutboxStatus.SENT)
    event.dispatched_at = now.astimezone(UTC)
    event.last_error = None
    return "sent"


def dispatch_event(
    session: Session,
    event_id: str,
    *,
    publisher: EventPublisher,
    settings: Settings,
    now: datetime,
) -> str:
    """Dispatch one known event, locking its row. Used immediately after commit."""
    event = session.execute(
        select(OutboxEvent)
        .where(OutboxEvent.id == event_id, OutboxEvent.status == str(OutboxStatus.PENDING))
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if event is None:
        return "skipped"
    return _attempt(session, event, publisher=publisher, settings=settings, now=now)


def dispatch_due(
    session: Session,
    *,
    publisher: EventPublisher,
    settings: Settings,
    now: datetime,
    limit: int = 100,
) -> DispatchOutcome:
    """Retry sweep. Run on a schedule; safe to run in parallel (``skip_locked``)."""
    events = (
        session.execute(
            select(OutboxEvent)
            .where(
                OutboxEvent.status == str(OutboxStatus.PENDING),
                OutboxEvent.next_attempt_at <= now.astimezone(UTC),
            )
            .order_by(OutboxEvent.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    tally = {"sent": 0, "retried": 0, "dead_lettered": 0}
    for event in events:
        tally[_attempt(session, event, publisher=publisher, settings=settings, now=now)] += 1
    return DispatchOutcome(**tally)
