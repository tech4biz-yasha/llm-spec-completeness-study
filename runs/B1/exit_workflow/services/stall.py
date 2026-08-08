"""Stall detection — algorithm.md step 6, rules.yaml#EXIT-05.

    "Inspection must be scheduled within 30 days of move_out_date. Past that,
    the workflow enters STALLED and an admin task is created. It does not
    auto-cancel."

Run this from the scheduler. The window is counted in Asia/Dubai calendar days
(decision D-001, edges.yaml#X-007): a workflow stalls once the current Dubai day
is *past* move_out_date + 30, so day 30 itself is still within the window.

states.yaml gives STALLED an inbound edge from INSPECTION_SCHEDULED as well as
from OWNER_NOTIFIED, so a workflow whose inspection was scheduled but never
carried out stalls too. Both edges are honoured because both are in the spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from exit_workflow.config import STALL_THRESHOLD_DAYS
from exit_workflow.domain import clock as clock_module
from exit_workflow.domain.clock import Clock, DEFAULT_CLOCK, add_days
from exit_workflow.domain.enums import ActorRole, AdminTaskType
from exit_workflow.domain.states import State
from exit_workflow.repositories.admin_tasks import AdminTaskRepository
from exit_workflow.repositories.workflows import WorkflowRepository
from exit_workflow.services.transitions import apply_transition

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StallReport:
    stalled: list[str]

    @property
    def count(self) -> int:
        return len(self.stalled)


class StallService:
    """Moves overdue workflows to STALLED and opens the admin task."""

    def __init__(self, session: AsyncSession, *, clock: Clock = DEFAULT_CLOCK) -> None:
        self._session = session
        self._clock = clock

    async def scan(self, *, limit: int = 500) -> StallReport:
        today = clock_module.today_dubai(self._clock)
        # Stall once today is past move_out_date + 30 days, i.e. once
        # move_out_date is earlier than today - 30 days.
        cutoff = add_days(today, -STALL_THRESHOLD_DAYS)

        workflows = await WorkflowRepository(self._session).due_for_stall(cutoff)
        report = StallReport(stalled=[])

        for workflow in workflows[:limit]:
            source = State(workflow.status)
            await apply_transition(
                self._session,
                workflow,
                State.STALLED,
                actor_type=ActorRole.SYSTEM,
                actor_id=None,
                metadata={
                    "move_out_date": workflow.move_out_date.isoformat(),
                    "days_past_move_out": (today - workflow.move_out_date).days,
                    "threshold_days": STALL_THRESHOLD_DAYS,
                    "stalled_from": str(source),
                },
            )
            workflow.stalled_at = clock_module.now_utc(self._clock)
            AdminTaskRepository(self._session).open_task(
                task_type=AdminTaskType.EXIT_STALLED,
                workflow_id=workflow.id,
                payload={
                    "reason": "inspection not completed within 30 days of move-out",
                    "stalled_from": str(source),
                    "move_out_date": workflow.move_out_date.isoformat(),
                    "rule": "EXIT-05",
                },
            )
            report.stalled.append(workflow.id)
            logger.warning(
                "exit workflow %s stalled: %d days past move-out date %s",
                workflow.id,
                (today - workflow.move_out_date).days,
                workflow.move_out_date,
            )

        await self._session.flush()
        return report


async def run_stall_scan(
    session_factory: async_sessionmaker[AsyncSession], *, clock: Clock = DEFAULT_CLOCK
) -> StallReport:
    """Scheduler entry point: one scan in its own transaction."""
    async with session_factory() as session:
        async with session.begin():
            return await StallService(session, clock=clock).scan()
