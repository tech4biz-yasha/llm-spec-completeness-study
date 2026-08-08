"""Exit workflow document access."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Sequence as SASequence
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import ExitWorkflow
from exit_workflow.domain.ids import SEQUENCE_NAME, format_workflow_id
from exit_workflow.domain.states import State


class WorkflowRepository:
    """Reads and writes the workflow document."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_workflow_id(self, assigned_on: date) -> str:
        """Draw the next ID (rules.yaml#EXIT-02: sequence from PostgreSQL).

        ``nextval`` is non-transactional by design: a rolled-back initiation
        burns a number rather than handing the same ID to two tenants.
        """
        sequence_value = await self._session.scalar(SASequence(SEQUENCE_NAME).next_value())
        return format_workflow_id(assigned_on, int(sequence_value))

    async def get(self, workflow_id: str, *, for_update: bool = False) -> ExitWorkflow | None:
        statement = select(ExitWorkflow).where(ExitWorkflow.id == workflow_id)
        if for_update:
            # edges.yaml#X-005 — serialises concurrent settlement attempts.
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def get_by_contract(self, contract_id: uuid.UUID) -> ExitWorkflow | None:
        """rules.yaml#EXIT-01 — at most one workflow per contract."""
        return await self._session.scalar(
            select(ExitWorkflow).where(ExitWorkflow.contract_id == contract_id)
        )

    async def get_active_for_property(self, property_id: uuid.UUID) -> ExitWorkflow | None:
        """The workflow holding a property's exit lock, if any (edges.yaml#X-006)."""
        return await self._session.scalar(
            select(ExitWorkflow)
            .where(
                ExitWorkflow.property_id == property_id,
                ExitWorkflow.status != State.COMPLETE,
            )
            .order_by(ExitWorkflow.created_at)
            .limit(1)
        )

    def add(self, workflow: ExitWorkflow) -> ExitWorkflow:
        self._session.add(workflow)
        return workflow

    async def due_for_stall(self, cutoff_move_out_date: date) -> list[ExitWorkflow]:
        """Workflows past the stall threshold (rules.yaml#EXIT-05).

        Only the two states states.yaml gives a ``-> STALLED`` edge for:
        OWNER_NOTIFIED and INSPECTION_SCHEDULED.
        """
        stallable = tuple(
            transition.source
            for transition in _stall_transitions()
        )
        result = await self._session.scalars(
            select(ExitWorkflow)
            .where(
                ExitWorkflow.status.in_([str(state) for state in stallable]),
                ExitWorkflow.move_out_date < cutoff_move_out_date,
            )
            .order_by(ExitWorkflow.move_out_date)
            .with_for_update(skip_locked=True)
        )
        return list(result)


def _stall_transitions():
    """The states.yaml edges into STALLED, so the scan cannot drift from the spec."""
    from exit_workflow.domain.states import exit_workflow_machine

    return tuple(
        transition
        for transition in exit_workflow_machine().transitions
        if transition.target is State.STALLED
    )
