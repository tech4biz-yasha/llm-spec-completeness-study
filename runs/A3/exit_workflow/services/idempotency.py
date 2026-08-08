"""Idempotency-Key handling for money-moving endpoints."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.errors import IdempotencyConflictError, IdempotencyKeyReuseError
from exit_workflow.core.serialization import jsonable
from exit_workflow.models.idempotency import IdempotencyRecord


def hash_request(payload: Any) -> str:
    canonical = json.dumps(jsonable(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Replay:
    status_code: int
    body: dict[str, Any]


class IdempotencyService:
    """Reserve → work → complete, all inside the request transaction.

    A duplicate arriving while the first request is still open blocks on the
    primary key and then sees the committed row, so it replays rather than
    performing the work twice.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin(
        self,
        *,
        scope: str,
        key: str,
        request_hash: str,
        principal_id: uuid.UUID | None,
    ) -> Replay | None:
        existing = await self._session.get(IdempotencyRecord, (scope, key))
        if existing is not None:
            return self._replay_or_raise(existing, request_hash)

        record = IdempotencyRecord(
            scope=scope,
            key=key,
            request_hash=request_hash,
            principal_id=principal_id,
            created_at=utcnow(),
        )
        self._session.add(record)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            # Another request won the race; re-read it now that it is visible.
            self._session.expunge(record)
            committed = (
                await self._session.execute(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.scope == scope, IdempotencyRecord.key == key
                    )
                )
            ).scalars().first()
            if committed is None:  # pragma: no cover - defensive
                raise IdempotencyConflictError(
                    "A concurrent request with the same Idempotency-Key is in progress."
                ) from None
            return self._replay_or_raise(committed, request_hash)
        return None

    @staticmethod
    def _replay_or_raise(record: IdempotencyRecord, request_hash: str) -> Replay:
        if record.request_hash != request_hash:
            raise IdempotencyKeyReuseError(
                "This Idempotency-Key was already used with a different request body."
            )
        if record.completed_at is None or record.response_status is None:
            raise IdempotencyConflictError(
                "A request with this Idempotency-Key is still in progress; retry shortly.",
                headers={"Retry-After": "2"},
            )
        return Replay(status_code=record.response_status, body=record.response_body or {})

    async def complete(
        self, *, scope: str, key: str, status_code: int, body: dict[str, Any]
    ) -> None:
        record = await self._session.get(IdempotencyRecord, (scope, key))
        if record is None:  # pragma: no cover - begin() always creates it
            return
        record.response_status = status_code
        record.response_body = jsonable(body)
        record.completed_at = utcnow()
