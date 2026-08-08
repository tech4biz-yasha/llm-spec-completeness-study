"""Relays outbox rows to the event bus.

Runs as an asyncio task inside the API process by default; set
``EXITFLOW_ENABLE_BACKGROUND_WORKERS=false`` and run ``python -m app.workers.run`` to
operate it as a separate deployment. Several replicas are safe: rows are claimed with
``FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import session_scope
from app.domain.enums import OutboxStatus
from app.ports.event_publisher import EventPublisher, OutgoingEvent
from app.repositories.support import OutboxRepository

log = get_logger(__name__)

#: Retry backoff by attempt count, capped. Deliberately coarse -- the outbox is a
#: durability mechanism, not a low-latency path.
_BACKOFF_SECONDS = (1, 5, 15, 60, 300, 900, 1800, 3600)


def backoff_for(attempts: int) -> timedelta:
    index = min(max(attempts - 1, 0), len(_BACKOFF_SECONDS) - 1)
    return timedelta(seconds=_BACKOFF_SECONDS[index])


class OutboxDispatcher:
    def __init__(
        self,
        publisher: EventPublisher,
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._publisher = publisher
        self._settings = settings or get_settings()
        self._clock = clock or SystemClock()
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        await self._publisher.start()
        log.info("outbox.dispatcher.started", topic=self._settings.kafka_topic)
        try:
            while not self._stopping.is_set():
                try:
                    published = await self.run_once()
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("outbox.dispatcher.cycle_failed")
                    published = 0
                if published == 0:
                    with_timeout = self._settings.outbox_poll_interval_seconds
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=with_timeout)
                    except TimeoutError:
                        pass
        finally:
            await self._publisher.stop()
            log.info("outbox.dispatcher.stopped")

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self) -> int:
        """Publish one batch. Returns the number of messages published."""
        published = 0
        async with session_scope() as session:
            repo = OutboxRepository(session)
            now = self._clock.now()
            batch = await repo.claim_batch(now=now, limit=self._settings.outbox_batch_size)
            for message in batch:
                event = OutgoingEvent(
                    topic=message.topic,
                    key=message.partition_key,
                    value=message.payload,
                    headers={
                        **{str(k): str(v) for k, v in (message.headers or {}).items()},
                        "event-id": str(message.event_id),
                    },
                )
                try:
                    await self._publisher.publish(event)
                except Exception as exc:  # noqa: BLE001 - any failure is retryable
                    message.attempts += 1
                    message.last_error = str(exc)[:1000]
                    if message.attempts >= self._settings.outbox_max_attempts:
                        message.status = OutboxStatus.DEAD_LETTER
                        log.error(
                            "outbox.message.dead_lettered",
                            event_id=str(message.event_id),
                            event_type=message.event_type,
                            attempts=message.attempts,
                        )
                    else:
                        message.status = OutboxStatus.FAILED
                        message.available_at = now + backoff_for(message.attempts)
                        log.warning(
                            "outbox.message.retry_scheduled",
                            event_id=str(message.event_id),
                            attempts=message.attempts,
                            error=str(exc)[:200],
                        )
                else:
                    message.status = OutboxStatus.PUBLISHED
                    message.published_at = now
                    message.last_error = None
                    published += 1
        return published

    async def purge(self, *, older_than_days: int = 7) -> int:
        """Drop published rows past their usefulness. The audit trail is the record."""
        async with session_scope() as session:
            repo = OutboxRepository(session)
            return await repo.purge_published(
                before=self._clock.now() - timedelta(days=older_than_days)
            )
