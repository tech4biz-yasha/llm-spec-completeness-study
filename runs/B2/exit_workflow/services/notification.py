"""Owner notification — rules.yaml#EXIT-04, edges.yaml#X-002.

    "Owner notification is emitted AFTER the initiation transaction commits. If
     dispatch fails, the workflow does NOT roll back; the event queues for retry
     with exponential backoff, 5 attempts, then dead-letter + admin alert."

Implemented as a transactional outbox: the event row is written inside the
initiation transaction (so it can never be lost if the process dies between
commit and publish), and published only after that transaction commits. Nothing
in this file can roll the workflow back — dispatch runs in its own session and
never propagates a publish error to the initiating request.

The DOCS_SUBMITTED -> OWNER_NOTIFIED transition (states.yaml, side_effect
notify_owner) is applied when the event is successfully published.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..clock import Clock
from ..config import Settings
from ..db.models import (
    AdminTask,
    AdminTaskType,
    ExitWorkflow,
    OutboxEvent,
    OutboxStatus,
)
from ..db.session import transaction
from ..domain.states import State
from ..errors import ExitWorkflowError
from ..money import from_minor
from ..ports import EventPublisher, OutboundEvent
from .transitions import SYSTEM_PRINCIPAL, TransitionService

logger = logging.getLogger(__name__)

OWNER_NOTIFICATION_EVENT_TYPE = "exit_workflow.owner_notification_requested"


class NotificationService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        transitions: TransitionService,
        clock: Clock,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._transitions = transitions
        self._clock = clock
        self._settings = settings

    # -- write side (inside the initiation transaction) ----------------------

    async def enqueue_owner_notification(
        self, session: AsyncSession, workflow: ExitWorkflow
    ) -> uuid.UUID:
        """Persist the event with the workflow. rules.yaml#EXIT-04."""
        event = OutboxEvent(
            id=uuid.uuid4(),
            workflow_id=workflow.id,
            topic=self._settings.kafka_topic_owner_notification,
            event_type=OWNER_NOTIFICATION_EVENT_TYPE,
            partition_key=str(workflow.owner_id),
            payload={
                "event_type": OWNER_NOTIFICATION_EVENT_TYPE,
                "workflow_id": workflow.id,
                "contract_id": str(workflow.contract_id),
                "property_id": str(workflow.property_id),
                "owner_id": str(workflow.owner_id),
                "tenant_id": str(workflow.tenant_id),
                # edges.yaml#X-007 — Dubai calendar day, serialised as a date.
                "move_out_date": workflow.move_out_date.isoformat(),
                "reason": workflow.reason,
                "security_deposit": str(from_minor(workflow.security_deposit_minor)),
                "currency": "AED",
                "occurred_at": self._clock.now_utc().isoformat(),
            },
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=self._clock.now_utc(),
            created_at=self._clock.now_utc(),
        )
        session.add(event)
        return event.id

    # -- dispatch side (always after commit, never inside a request txn) -----

    async def dispatch_now(self, event_id: uuid.UUID) -> bool:
        """First delivery attempt, immediately after the initiation commit.

        Returns True if published. Never raises: rules.yaml#EXIT-04 says the
        workflow does not roll back on dispatch failure, so the caller is not
        told about one — the retry queue owns it from here.
        """
        try:
            return await self._attempt(event_id)
        except Exception:  # noqa: BLE001 — a dispatch fault must never reach the caller
            logger.exception("owner notification dispatch failed", extra={"event_id": str(event_id)})
            return False

    async def dispatch_pending(self, limit: int = 100) -> int:
        """Retry sweep. Run from a worker/scheduler. Returns events published."""
        now = self._clock.now_utc()
        async with transaction(self._session_factory) as session:
            due = (
                await session.execute(
                    select(OutboxEvent.id)
                    .where(OutboxEvent.status == OutboxStatus.PENDING)
                    .where(OutboxEvent.next_attempt_at <= now)
                    .order_by(OutboxEvent.next_attempt_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).scalars().all()

        published = 0
        for event_id in due:
            if await self.dispatch_now(event_id):
                published += 1
        return published

    async def _attempt(self, event_id: uuid.UUID) -> bool:
        async with transaction(self._session_factory) as session:
            event = (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                )
            ).scalar_one_or_none()
            if event is None or event.status is not OutboxStatus.PENDING:
                return False
            outbound = OutboundEvent(
                topic=event.topic,
                key=event.partition_key,
                event_type=event.event_type,
                payload=event.payload,
            )
            attempts = event.attempts + 1

        try:
            await self._publisher.publish(outbound)
        except Exception as exc:  # noqa: BLE001 — any producer error is a dispatch failure
            await self._record_failure(event_id, attempts, exc)
            return False

        await self._record_success(event_id)
        return True

    async def _record_success(self, event_id: uuid.UUID) -> None:
        async with transaction(self._session_factory) as session:
            event = (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                )
            ).scalar_one()
            if event.status is OutboxStatus.PUBLISHED:
                return
            event.status = OutboxStatus.PUBLISHED
            event.published_at = self._clock.now_utc()
            event.attempts += 1

            workflow = (
                await session.execute(
                    select(ExitWorkflow)
                    .where(ExitWorkflow.id == event.workflow_id)
                    .with_for_update()
                )
            ).scalar_one()
            # states.yaml: DOCS_SUBMITTED -> OWNER_NOTIFIED, actor system,
            # side_effect notify_owner (rules.yaml#EXIT-04). Applied once the
            # notification is actually on the wire. If the workflow has already
            # moved on (a retry landing late), leave it alone rather than
            # forcing an undeclared transition.
            if workflow.status is State.DOCS_SUBMITTED:
                await self._transitions.apply(
                    session,
                    workflow,
                    State.OWNER_NOTIFIED,
                    SYSTEM_PRINCIPAL,
                    metadata={"event_id": str(event.id), "topic": event.topic},
                )

    async def _record_failure(
        self, event_id: uuid.UUID, attempts: int, exc: BaseException
    ) -> None:
        async with transaction(self._session_factory) as session:
            event = (
                await session.execute(
                    select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
                )
            ).scalar_one()
            event.attempts = attempts
            event.last_error = f"{type(exc).__name__}: {exc}"[:2000]

            if attempts >= self._settings.notification_max_attempts:
                # rules.yaml#EXIT-04 — 5 attempts, then dead-letter + admin alert.
                event.status = OutboxStatus.DEAD_LETTERED
                event.next_attempt_at = self._clock.now_utc()
                await self._open_admin_task(session, event)
                logger.error(
                    "owner notification dead-lettered",
                    extra={"event_id": str(event_id), "workflow_id": event.workflow_id},
                )
            else:
                event.next_attempt_at = self._clock.now_utc() + self._backoff(attempts)

    def _backoff(self, attempts: int) -> timedelta:
        """rules.yaml#EXIT-04 — exponential backoff between the 5 attempts."""
        seconds = min(
            self._settings.notification_backoff_base_seconds**attempts,
            self._settings.notification_backoff_cap_seconds,
        )
        return timedelta(seconds=seconds)

    async def _open_admin_task(self, session: AsyncSession, event: OutboxEvent) -> None:
        """rules.yaml#EXIT-04 — the "admin alert" half of the dead-letter rule.

        blockers.md#B-009: the spec defines no recovery path for a workflow whose
        owner notification dead-letters; it stays in DOCS_SUBMITTED and the admin
        task is the only handle on it.
        """
        stmt = (
            pg_insert(AdminTask)
            .values(
                id=uuid.uuid4(),
                workflow_id=event.workflow_id,
                task_type=AdminTaskType.NOTIFICATION_DEAD_LETTER.value,
                status="OPEN",
                details={
                    "event_id": str(event.id),
                    "topic": event.topic,
                    "attempts": event.attempts,
                    "last_error": event.last_error,
                },
                created_at=self._clock.now_utc(),
            )
            # Matches the partial index uq_admin_task_open: one OPEN task of a
            # type per workflow, so an at-least-once dispatcher cannot pile up
            # duplicates.
            .on_conflict_do_nothing(
                index_elements=[AdminTask.workflow_id, AdminTask.task_type],
                index_where=text("status = 'OPEN'"),
            )
        )
        await session.execute(stmt)


class NotificationDispatchError(ExitWorkflowError):
    """Only used by tests/fakes to simulate a producer failure."""
