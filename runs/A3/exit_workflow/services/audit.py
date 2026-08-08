"""Audit recording (A3: complete trails, 7-year retention)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.serialization import jsonable
from exit_workflow.models.audit import AuditEvent
from exit_workflow.services.context import ServiceContext


class AuditRecorder:
    """Appends audit rows to the *current* transaction.

    Because the audit write shares the business transaction, an action that
    rolls back leaves no audit row claiming it happened — and an action that
    commits can never be missing one.
    """

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def record(
        self,
        ctx: ServiceContext,
        *,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        workflow_id: uuid.UUID | None = None,
        changes: dict[str, Any] | None = None,
    ) -> AuditEvent:
        now = utcnow()
        event = AuditEvent(
            occurred_at=now,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            workflow_id=workflow_id,
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            actor_email=ctx.actor_email,
            request_id=ctx.request_id,
            ip_address=ctx.ip_address,
            user_agent=(ctx.user_agent or None) and ctx.user_agent[:512],
            changes=jsonable(changes or {}),
            retention_until=date(
                now.year + self._settings.audit_retention_years, now.month, min(now.day, 28)
            ),
        )
        self._session.add(event)
        return event
