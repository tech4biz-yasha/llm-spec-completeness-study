"""Audit trail (rules.yaml#EXIT-10).

Append-only is enforced by the database trigger installed in
``migrations/001_initial.sql``; this repository offers no update or delete, so
the two agree.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import ExitWorkflowAudit
from exit_workflow.domain.enums import ActorRole
from exit_workflow.domain.states import State


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def append(
        self,
        *,
        workflow_id: str,
        actor_type: ActorRole,
        actor_id: str | None,
        from_state: State | None,
        to_state: State,
        rule_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExitWorkflowAudit:
        """Write one audit row. rules.yaml#EXIT-10: actor, timestamp, from, to, metadata.

        No flush here: the row joins whatever transaction the caller is running,
        which is what "IN ONE TRANSACTION ... write audit row" (algorithm.md
        steps 4 and 13) requires.
        """
        row = ExitWorkflowAudit(
            workflow_id=workflow_id,
            actor_type=str(actor_type),
            actor_id=actor_id,
            from_state=None if from_state is None else str(from_state),
            to_state=str(to_state),
            rule_id=rule_id,
            audit_metadata=metadata or {},
        )
        self._session.add(row)
        return row

    async def for_workflow(self, workflow_id: str) -> list[ExitWorkflowAudit]:
        result = await self._session.scalars(
            select(ExitWorkflowAudit)
            .where(ExitWorkflowAudit.workflow_id == workflow_id)
            .order_by(ExitWorkflowAudit.id)
        )
        return list(result)
