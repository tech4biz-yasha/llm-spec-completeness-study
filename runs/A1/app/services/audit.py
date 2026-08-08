"""Audit trail writer (SRS A3: complete trails, seven-year retention)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, AuditLogEntry
from app.services.context import RequestContext


def _retain_until(years: int) -> date:
    today = date.today()
    try:
        return today.replace(year=today.year + years)
    except ValueError:
        # 29 February in a leap year -> 28 February in the (non-leap) retention year.
        return today.replace(year=today.year + years, day=28)


class AuditService:
    """Appends audit rows to the caller's transaction.

    Audit rows are written in the *same* transaction as the change they describe, so the
    trail can never disagree with the data.
    """

    def __init__(self, session: AsyncSession, ctx: RequestContext, *, retention_years: int = 7):
        self._session = session
        self._ctx = ctx
        self._retention_years = retention_years

    def record(
        self,
        action: AuditAction,
        *,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        workflow_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditLogEntry:
        entry = self._build(
            action,
            entity_type=entity_type,
            entity_id=entity_id,
            workflow_id=workflow_id,
            payload=payload,
        )
        self._session.add(entry)
        return entry

    async def record_out_of_band(
        self,
        action: AuditAction,
        *,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        workflow_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Persist an audit row in its own transaction.

        Used for events that must be recorded even though the caller's transaction is about
        to roll back — a contract creation blocked by BR-1 is the motivating case: the
        attempt is exactly what compliance needs to see, but nothing else about the request
        should be committed.
        """
        from app.db import get_sessionmaker

        entry = self._build(
            action,
            entity_type=entity_type,
            entity_id=entity_id,
            workflow_id=workflow_id,
            payload=payload,
        )
        factory = get_sessionmaker()
        async with factory() as session, session.begin():
            session.add(entry)

    def _build(
        self,
        action: AuditAction,
        *,
        entity_type: str,
        entity_id: uuid.UUID | None,
        workflow_id: uuid.UUID | None,
        payload: dict[str, Any] | None,
    ) -> AuditLogEntry:
        return AuditLogEntry(
            action=action,
            actor_type=self._ctx.actor_type,
            actor_id=self._ctx.actor_id,
            entity_type=entity_type,
            entity_id=entity_id,
            workflow_id=workflow_id,
            request_id=self._ctx.request_id,
            ip_address=self._ctx.ip_address,
            user_agent=(self._ctx.user_agent or None),
            payload=payload or {},
            retain_until=_retain_until(self._retention_years),
        )
