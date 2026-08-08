"""Stall sweep — algorithm.md step 6, rules.yaml#EXIT-05.

    "Inspection must be scheduled within 30 days of move_out_date. Past that,
     the workflow enters STALLED and an admin task is created. It does not
     auto-cancel."

states.yaml declares the timer for two source states, with the same predicate
`when: 30_days_past_move_out`:

    OWNER_NOTIFIED       -> STALLED
    INSPECTION_SCHEDULED -> STALLED

Run this from a scheduler. It is idempotent and safe to run concurrently.

blockers.md#B-003: states.yaml declares no transition *out of* STALLED, and
forbids STALLED -> COMPLETE. A stalled workflow therefore has no specified
route back into the flow and holds property.exitLock indefinitely; the admin
task is the only handle on it. Not resolved here.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import text

from ..clock import Clock
from ..db.models import AdminTask, AdminTaskType, ExitWorkflow
from ..db.session import transaction
from ..domain.rules import STALLABLE_STATES, is_past_stall_window, stall_deadline
from ..domain.states import State
from .transitions import SYSTEM_PRINCIPAL, TransitionService

logger = logging.getLogger(__name__)


class StallService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        transitions: TransitionService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._transitions = transitions

    async def sweep(self, *, limit: int = 500) -> list[str]:
        """Move every overdue workflow to STALLED. Returns the ids moved."""
        today = self._clock.today_dubai()  # D-001 / edges.yaml#X-007
        # Only rows whose deadline has already passed are candidates; the exact
        # comparison is re-applied per row against the Dubai calendar.
        async with transaction(self._session_factory) as session:
            candidates = (
                await session.execute(
                    select(ExitWorkflow.id)
                    .where(ExitWorkflow.status.in_(list(STALLABLE_STATES)))
                    .where(ExitWorkflow.move_out_date < today)
                    .order_by(ExitWorkflow.move_out_date)
                    .limit(limit)
                )
            ).scalars().all()

        stalled: list[str] = []
        for workflow_id in candidates:
            if await self._stall_one(workflow_id):
                stalled.append(workflow_id)
        return stalled

    async def _stall_one(self, workflow_id: str) -> bool:
        async with transaction(self._session_factory) as session:
            workflow = (
                await session.execute(
                    select(ExitWorkflow).where(ExitWorkflow.id == workflow_id).with_for_update()
                )
            ).scalar_one()

            if workflow.status not in STALLABLE_STATES:
                return False
            # rules.yaml#EXIT-05 — 30 days past move_out_date, Dubai calendar.
            if not is_past_stall_window(workflow.move_out_date, self._clock.today_dubai()):
                return False

            previous_status = workflow.status
            await self._transitions.apply(
                session,
                workflow,
                State.STALLED,
                SYSTEM_PRINCIPAL,
                metadata={
                    "move_out_date": workflow.move_out_date.isoformat(),
                    "deadline": stall_deadline(workflow.move_out_date).isoformat(),
                    "reason": "30_days_past_move_out",
                },
            )
            workflow.stalled_at = self._clock.now_utc()

            # rules.yaml#EXIT-05 — "an admin task is created. It does not auto-cancel."
            await self._open_admin_task(session, workflow, previous_status)
            logger.warning(
                "exit workflow stalled",
                extra={"workflow_id": workflow.id, "move_out_date": str(workflow.move_out_date)},
            )
            return True

    async def _open_admin_task(
        self, session: AsyncSession, workflow: ExitWorkflow, previous_status: State
    ) -> None:
        await session.execute(
            pg_insert(AdminTask)
            .values(
                id=uuid.uuid4(),
                workflow_id=workflow.id,
                task_type=AdminTaskType.STALLED_EXIT.value,
                status="OPEN",
                details={
                    "move_out_date": workflow.move_out_date.isoformat(),
                    "deadline": stall_deadline(workflow.move_out_date).isoformat(),
                    "previous_status": previous_status.value,
                },
                created_at=self._clock.now_utc(),
            )
            .on_conflict_do_nothing(
                index_elements=[AdminTask.workflow_id, AdminTask.task_type],
                index_where=text("status = 'OPEN'"),
            )
        )
