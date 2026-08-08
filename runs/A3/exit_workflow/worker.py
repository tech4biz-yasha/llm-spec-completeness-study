"""Background worker: outbox relay and notification dispatch.

Both drains use ``FOR UPDATE SKIP LOCKED`` so several application instances can
run the worker concurrently without double-publishing or double-sending.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.logging import get_logger
from exit_workflow.domain.enums import NotificationStatus, OutboxStatus
from exit_workflow.models.notification import NotificationLog
from exit_workflow.models.outbox import OutboxEvent
from exit_workflow.services.events import EventPublisher, topic_for
from exit_workflow.services.notifications import EmailSender

log = get_logger(__name__)

MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True, slots=True)
class TickResult:
    events_published: int = 0
    events_failed: int = 0
    notifications_sent: int = 0
    notifications_failed: int = 0

    @property
    def did_work(self) -> bool:
        return bool(
            self.events_published
            or self.events_failed
            or self.notifications_sent
            or self.notifications_failed
        )


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(2**attempts, MAX_BACKOFF_SECONDS))


class BackgroundWorker:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        publisher: EventPublisher,
        email_sender: EmailSender,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._publisher = publisher
        self._email_sender = email_sender
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self._publisher.start()
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="exit-workflow-worker")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutdown path
                pass
            self._task = None
        await self._publisher.stop()

    async def _run(self) -> None:
        interval = self._settings.outbox_poll_interval_seconds
        while not self._stopping.is_set():
            try:
                result = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the worker must not die
                log.error("worker_tick_failed", error=str(exc), exc_info=True)
                result = TickResult()
            # Drain greedily while there is work, then idle politely.
            await asyncio.sleep(0 if result.did_work else interval)

    # -- work --------------------------------------------------------------
    async def tick(self) -> TickResult:
        published, failed = await self._drain_outbox()
        sent, send_failed = await self._drain_notifications()
        return TickResult(published, failed, sent, send_failed)

    async def _drain_outbox(self) -> tuple[int, int]:
        published = failed = 0
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    (
                        await session.execute(
                            select(OutboxEvent)
                            .where(
                                OutboxEvent.status == OutboxStatus.PENDING,
                                OutboxEvent.available_at <= utcnow(),
                            )
                            .order_by(OutboxEvent.occurred_at)
                            .limit(self._settings.outbox_batch_size)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for event in rows:
                    try:
                        await self._publisher.publish(
                            topic_for(self._settings, event.aggregate_type),
                            event.partition_key,
                            json.dumps(event.payload, separators=(",", ":")).encode("utf-8"),
                            {k: str(v) for k, v in (event.headers or {}).items() if v is not None},
                        )
                    except Exception as exc:  # noqa: BLE001 - publisher-defined failures
                        event.attempts += 1
                        event.last_error = str(exc)[:2000]
                        if event.attempts >= self._settings.outbox_max_attempts:
                            event.status = OutboxStatus.FAILED
                            log.error(
                                "outbox_event_dead_lettered",
                                event_id=str(event.id),
                                event_type=event.event_type,
                                attempts=event.attempts,
                            )
                        else:
                            event.available_at = utcnow() + _backoff(event.attempts)
                        failed += 1
                    else:
                        event.status = OutboxStatus.PUBLISHED
                        event.published_at = utcnow()
                        published += 1
        return published, failed

    async def _drain_notifications(self) -> tuple[int, int]:
        sent = failed = 0
        async with self._session_factory() as session:
            async with session.begin():
                rows = (
                    (
                        await session.execute(
                            select(NotificationLog)
                            .where(
                                NotificationLog.status == NotificationStatus.PENDING,
                                NotificationLog.available_at <= utcnow(),
                            )
                            .order_by(NotificationLog.created_at)
                            .limit(self._settings.notification_batch_size)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for row in rows:
                    try:
                        reference = await self._email_sender.send(
                            row.recipient, row.subject, row.body
                        )
                    except Exception as exc:  # noqa: BLE001 - provider-defined failures
                        row.attempts += 1
                        row.last_error = str(exc)[:2000]
                        if row.attempts >= self._settings.outbox_max_attempts:
                            row.status = NotificationStatus.FAILED
                        else:
                            row.available_at = utcnow() + _backoff(row.attempts)
                        failed += 1
                    else:
                        row.status = NotificationStatus.SENT
                        row.sent_at = utcnow()
                        row.provider_reference = reference
                        sent += 1
        return sent, failed
