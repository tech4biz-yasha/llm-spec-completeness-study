"""Idempotency-Key handling for unsafe endpoints."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.errors import ConflictError, IdempotencyConflictError, ValidationFailedError
from app.core.security import hash_request_body
from app.models.idempotency import IdempotencyRecord
from app.repositories.support import IdempotencyRepository

RETENTION = timedelta(hours=24)


@dataclass(slots=True)
class Replay:
    """A previously completed request whose stored response should be returned."""

    status_code: int
    body: dict[str, Any]


class IdempotencyGuard:
    def __init__(self, session: AsyncSession, clock: Clock) -> None:
        self._session = session
        self._clock = clock
        self._repo = IdempotencyRepository(session)
        self._record: IdempotencyRecord | None = None

    async def begin(
        self,
        *,
        key: str | None,
        endpoint: str,
        actor_id: uuid.UUID | None,
        workflow_id: uuid.UUID | None,
        body: bytes,
        required: bool = False,
    ) -> Replay | None:
        """Reserve the key, or return the stored response for a replay.

        The reservation is inserted inside a SAVEPOINT so a duplicate key leaves the
        surrounding transaction usable. A concurrent duplicate blocks on the unique
        index until the first request commits, and then takes the replay path.
        """
        if key is None:
            if required:
                raise ValidationFailedError(
                    "This endpoint requires an Idempotency-Key header.",
                    details={"header": "Idempotency-Key"},
                )
            return None
        if len(key) < 8:
            raise ValidationFailedError(
                "Idempotency-Key must be at least 8 characters.",
                details={"header": "Idempotency-Key"},
            )

        now = self._clock.now()
        request_hash = hash_request_body(body)
        record = IdempotencyRecord(
            id=uuid.uuid4(),
            idempotency_key=key,
            endpoint=endpoint,
            actor_id=actor_id,
            workflow_id=workflow_id,
            request_hash=request_hash,
            created_at=now,
            expires_at=now + RETENTION,
        )

        try:
            async with self._session.begin_nested():
                self._repo.add(record)
                await self._session.flush()
        except IntegrityError:
            existing = await self._repo.find(key, endpoint)
            if existing is None:  # pragma: no cover - the unique index says otherwise
                raise
            if existing.request_hash != request_hash:
                raise IdempotencyConflictError() from None
            if existing.response_status is None:
                raise ConflictError(
                    "An identical request is still being processed. Retry shortly.",
                    code="idempotent_request_in_flight",
                    details={"retry_after_seconds": 2},
                ) from None
            return Replay(
                status_code=existing.response_status, body=existing.response_body or {}
            )

        self._record = record
        return None

    def complete(self, *, status_code: int, body: dict[str, Any]) -> None:
        """Store the response so a later replay of the same key returns it verbatim."""
        if self._record is None:
            return
        self._record.response_status = status_code
        self._record.response_body = body
        self._record.completed_at = self._clock.now()
