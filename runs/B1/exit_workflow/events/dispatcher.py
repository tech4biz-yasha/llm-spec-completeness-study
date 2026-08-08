"""Outbox dispatcher — rules.yaml#EXIT-04.

    "Owner notification is emitted AFTER the initiation transaction commits. If
    dispatch fails, the workflow does NOT roll back; the event queues for retry
    with exponential backoff, 5 attempts, then dead-letter + admin alert."

edges.yaml#X-002 restates the consequence: the workflow never rolls back.

Each event is handled in its own transaction so a poison event cannot stall the
rest of the queue, and rows are claimed with ``FOR UPDATE SKIP LOCKED`` so
several dispatcher replicas can run at once.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from exit_workflow.config import NOTIFICATION_MAX_ATTEMPTS, Settings, get_settings
from exit_workflow.db.models import EventOutbox
from exit_workflow.domain import clock as clock_module
from exit_workflow.domain.clock import Clock, DEFAULT_CLOCK
from exit_workflow.domain.enums import ActorRole, AdminTaskType, EventType, OutboxStatus
from exit_workflow.domain.states import State
from exit_workflow.events.publisher import EventPublisher, PublishError
from exit_workflow.repositories.admin_tasks import AdminTaskRepository
from exit_workflow.repositories.outbox import OutboxRepository
from exit_workflow.repositories.workflows import WorkflowRepository
from exit_workflow.services.transitions import apply_transition

logger = logging.getLogger(__name__)


def backoff_delay_seconds(
    attempt: int,
    *,
    base_seconds: float,
    factor: float,
    max_seconds: float,
) -> float:
    """Exponential backoff for ``attempt`` (1 = after the first failure)."""
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    return min(base_seconds * (factor ** (attempt - 1)), max_seconds)


@dataclass(slots=True)
class DispatchReport:
    """Outcome of one dispatcher pass."""

    sent: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    dead_lettered: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return len(self.sent) + len(self.retried) + len(self.dead_lettered)


class OutboxDispatcher:
    """Publishes queued events and applies their post-dispatch side effects."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        *,
        settings: Settings | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._settings = settings or get_settings()
        self._clock = clock

    async def dispatch_due(self, *, limit: int = 100) -> DispatchReport:
        """Process up to ``limit`` events whose backoff has elapsed."""
        report = DispatchReport()
        for _ in range(limit):
            handled = await self._dispatch_one(report)
            if not handled:
                break
        return report

    async def dispatch_event(self, event_id: uuid.UUID) -> DispatchReport:
        """Attempt one specific event.

        Used immediately after the initiation transaction commits, so the owner
        notification goes out at once instead of waiting for the next background
        pass (rules.yaml#EXIT-04: emitted after commit).
        """
        report = DispatchReport()
        async with self._session_factory() as session:
            async with session.begin():
                event = await OutboxRepository(session).get(event_id, for_update=True)
                if event is None or event.status != str(OutboxStatus.PENDING):
                    return report
                await self._process(session, event, report)
        return report

    async def _dispatch_one(self, report: DispatchReport) -> bool:
        async with self._session_factory() as session:
            async with session.begin():
                now = clock_module.now_utc(self._clock)
                claimed = await OutboxRepository(session).claim_due(now=now, limit=1)
                if not claimed:
                    return False
                await self._process(session, claimed[0], report)
                return True

    async def _process(
        self, session: AsyncSession, event: EventOutbox, report: DispatchReport
    ) -> None:
        """Publish one claimed event and record the outcome.

        Runs inside the caller's transaction: the outbox row, the workflow
        transition and any admin task commit together, so the queue state can
        never disagree with the workflow state.
        """
        try:
            await self._publisher.publish(
                topic=event.topic, key=event.event_key, payload=event.payload
            )
        except PublishError as exc:
            await self._record_failure(session, event, str(exc), report)
            return

        event.status = str(OutboxStatus.SENT)
        event.dispatched_at = clock_module.now_utc(self._clock)
        event.attempts += 1
        event.last_error = None
        await self._apply_side_effect(session, event)
        report.sent.append(str(event.id))

    async def _record_failure(
        self,
        session: AsyncSession,
        event: EventOutbox,
        error: str,
        report: DispatchReport,
    ) -> None:
        """Reschedule or dead-letter. The workflow itself is never touched."""
        event.attempts += 1
        event.last_error = error[:2000]

        # rules.yaml#EXIT-04 — 5 attempts, then dead-letter + admin alert.
        if event.attempts >= NOTIFICATION_MAX_ATTEMPTS:
            event.status = str(OutboxStatus.DEAD_LETTER)
            AdminTaskRepository(session).open_task(
                task_type=AdminTaskType.NOTIFICATION_DEAD_LETTER,
                workflow_id=event.event_key,
                payload={
                    "event_id": str(event.id),
                    "event_type": event.event_type,
                    "topic": event.topic,
                    "attempts": event.attempts,
                    "last_error": event.last_error,
                    "rule": "EXIT-04",
                },
            )
            await self._publish_dead_letter(event)
            report.dead_lettered.append(str(event.id))
            logger.error(
                "event %s dead-lettered after %d attempts: %s",
                event.id,
                event.attempts,
                error,
            )
            return

        delay = backoff_delay_seconds(
            event.attempts,
            base_seconds=self._settings.notification_backoff_base_seconds,
            factor=self._settings.notification_backoff_factor,
            max_seconds=self._settings.notification_backoff_max_seconds,
        )
        event.next_attempt_at = clock_module.now_utc(self._clock) + timedelta(seconds=delay)
        report.retried.append(str(event.id))
        logger.warning(
            "event %s attempt %d/%d failed, retrying in %.1fs: %s",
            event.id,
            event.attempts,
            NOTIFICATION_MAX_ATTEMPTS,
            delay,
            error,
        )

    async def _publish_dead_letter(self, event: EventOutbox) -> None:
        """Best-effort copy to the dead-letter topic.

        The durable record of the failure is the DEAD_LETTER row plus the admin
        task, both of which commit with this transaction; if the broker is the
        thing that is down, this send will fail too and must not undo them.
        """
        try:
            await self._publisher.publish(
                topic=self._settings.kafka_dead_letter_topic,
                key=event.event_key,
                payload={
                    "original_topic": event.topic,
                    "event_type": event.event_type,
                    "attempts": event.attempts,
                    "last_error": event.last_error,
                    "payload": event.payload,
                },
            )
        except PublishError as exc:
            logger.error("dead-letter publish for event %s also failed: %s", event.id, exc)

    async def _apply_side_effect(self, session: AsyncSession, event: EventOutbox) -> None:
        """Advance the workflow for events that carry a state side effect.

        states.yaml gives DOCS_SUBMITTED -> OWNER_NOTIFIED the side effect
        ``notify_owner``, so the workflow enters OWNER_NOTIFIED when the
        notification has actually been handed to the broker — not before. A
        workflow whose notification is still retrying stays in DOCS_SUBMITTED,
        which is what edges.yaml#X-002 requires ("Workflow NEVER rolls back").
        """
        if event.event_type != str(EventType.EXIT_INITIATED_OWNER_NOTIFICATION):
            return

        workflow = await WorkflowRepository(session).get(event.event_key, for_update=True)
        if workflow is None:  # pragma: no cover - workflow rows are never deleted
            logger.error("outbox event %s references unknown workflow %s", event.id, event.event_key)
            return

        if workflow.status != str(State.DOCS_SUBMITTED):
            # Redelivery, or an admin moved the workflow on. Publishing is
            # at-least-once, so this is expected rather than exceptional.
            logger.info(
                "workflow %s is %s, not DOCS_SUBMITTED; no transition applied for event %s",
                workflow.id,
                workflow.status,
                event.id,
            )
            return

        await apply_transition(
            session,
            workflow,
            State.OWNER_NOTIFIED,
            actor_type=ActorRole.SYSTEM,
            actor_id=None,
            metadata={"event_id": str(event.id), "topic": event.topic},
        )
