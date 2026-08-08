"""Data access for the exit workflow aggregate."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.pagination import Cursor
from app.domain.enums import BLOCKING_STATES, ExitWorkflowState
from app.models.exit_workflow import ExitWorkflow, StateTransition

_BLOCKING = [s.value for s in sorted(BLOCKING_STATES)]


class ExitWorkflowRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------ reads
    async def get(self, workflow_id: uuid.UUID) -> ExitWorkflow | None:
        return await self._session.get(ExitWorkflow, workflow_id)

    async def require(self, workflow_id: uuid.UUID) -> ExitWorkflow:
        workflow = await self.get(workflow_id)
        if workflow is None:
            raise NotFoundError(
                "Exit workflow not found.", details={"workflow_id": str(workflow_id)}
            )
        return workflow

    async def get_for_update(self, workflow_id: uuid.UUID) -> ExitWorkflow:
        """Load the aggregate under a row lock.

        Every command path goes through here. Serialising writers on the aggregate row
        is what stops two concurrent 'Pay Deposit' clicks, or an approve racing a cancel,
        from both observing the same pre-state.
        """
        stmt = (
            select(ExitWorkflow)
            .where(ExitWorkflow.id == workflow_id)
            .with_for_update(of=ExitWorkflow)
            .execution_options(populate_existing=True)
        )
        workflow = (await self._session.execute(stmt)).scalar_one_or_none()
        if workflow is None:
            raise NotFoundError(
                "Exit workflow not found.", details={"workflow_id": str(workflow_id)}
            )
        return workflow

    async def get_by_reference(self, reference: str) -> ExitWorkflow | None:
        stmt = select(ExitWorkflow).where(ExitWorkflow.reference == reference)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------ BR-1
    async def find_blocking_for_property(
        self, property_id: uuid.UUID, *, exclude: uuid.UUID | None = None
    ) -> list[ExitWorkflow]:
        stmt = select(ExitWorkflow).where(
            ExitWorkflow.property_id == property_id,
            ExitWorkflow.state.in_(_BLOCKING),
        )
        if exclude is not None:
            stmt = stmt.where(ExitWorkflow.id != exclude)
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_blocking_for_tenant(
        self, tenant_id: uuid.UUID, *, exclude: uuid.UUID | None = None
    ) -> list[ExitWorkflow]:
        stmt = select(ExitWorkflow).where(
            ExitWorkflow.tenant_id == tenant_id,
            ExitWorkflow.state.in_(_BLOCKING),
        )
        if exclude is not None:
            stmt = stmt.where(ExitWorkflow.id != exclude)
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_blocking_for_contract(self, contract_id: uuid.UUID) -> ExitWorkflow | None:
        stmt = select(ExitWorkflow).where(
            ExitWorkflow.contract_id == contract_id,
            ExitWorkflow.state.in_(_BLOCKING),
        )
        return (await self._session.execute(stmt)).scalars().first()

    # ------------------------------------------------------------ lists
    async def list_page(
        self,
        *,
        tenant_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        property_id: uuid.UUID | None = None,
        agency_id: uuid.UUID | None = None,
        states: Sequence[ExitWorkflowState] | None = None,
        active_only: bool = False,
        cursor: Cursor | None = None,
        limit: int = 25,
    ) -> tuple[list[ExitWorkflow], bool]:
        """Keyset page over ``(created_at DESC, id DESC)``.

        Returns ``(items, has_more)``; one extra row is fetched to detect ``has_more``
        without a second COUNT query.
        """
        from app.models.inspection import Inspection  # noqa: PLC0415 - avoid import cycle

        stmt = select(ExitWorkflow)
        if tenant_id is not None:
            stmt = stmt.where(ExitWorkflow.tenant_id == tenant_id)
        if owner_id is not None:
            stmt = stmt.where(ExitWorkflow.owner_id == owner_id)
        if property_id is not None:
            stmt = stmt.where(ExitWorkflow.property_id == property_id)
        if agency_id is not None:
            stmt = stmt.where(
                ExitWorkflow.id.in_(
                    select(Inspection.workflow_id).where(Inspection.agency_id == agency_id)
                )
            )
        if states:
            stmt = stmt.where(ExitWorkflow.state.in_([s.value for s in states]))
        if active_only:
            stmt = stmt.where(ExitWorkflow.state.in_(_BLOCKING))
        if cursor is not None:
            stmt = stmt.where(
                or_(
                    ExitWorkflow.created_at < cursor.created_at,
                    (ExitWorkflow.created_at == cursor.created_at)
                    & (ExitWorkflow.id < cursor.id),
                )
            )

        stmt = stmt.order_by(ExitWorkflow.created_at.desc(), ExitWorkflow.id.desc()).limit(
            limit + 1
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        return rows[:limit], has_more

    # ---------------------------------------------------------- writes
    async def next_reference(self, year: int) -> str:
        """Allocate the human-facing Workflow ID (SRS T13 step 5).

        ``nextval`` is transactional-safe and never reuses a number, so references are
        unique even across rolled-back transactions -- gaps are acceptable, collisions
        are not.
        """
        value = (
            await self._session.execute(select(func.nextval("exit_workflow_reference_seq")))
        ).scalar_one()
        return f"EXW-{year}-{int(value):06d}"

    def add(self, workflow: ExitWorkflow) -> None:
        self._session.add(workflow)

    def record_transition(self, transition: StateTransition) -> None:
        self._session.add(transition)

    async def transitions_for(self, workflow_id: uuid.UUID) -> list[StateTransition]:
        stmt = (
            select(StateTransition)
            .where(StateTransition.workflow_id == workflow_id)
            .order_by(StateTransition.occurred_at, StateTransition.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # --------------------------------------------------- reconciliation
    async def find_stale(
        self, *, state: ExitWorkflowState, older_than: datetime, limit: int = 100
    ) -> list[ExitWorkflow]:
        stmt = (
            select(ExitWorkflow)
            .where(ExitWorkflow.state == state.value, ExitWorkflow.updated_at < older_than)
            .order_by(ExitWorkflow.updated_at)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())
