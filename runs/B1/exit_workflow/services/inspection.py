"""Inspection — algorithm.md steps 6 and 7.

    6. Owner schedules inspection. If 30 days pass beyond move_out_date first,
       system moves workflow to STALLED and opens admin task. (EXIT-05)
    7. Agency uploads damage_amount + photos -> INSPECTION_DONE.

The stall side of step 6 is time-driven and lives in
:mod:`exit_workflow.services.stall`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import ExitWorkflow
from exit_workflow.domain import clock as clock_module
from exit_workflow.domain.clock import Clock, DEFAULT_CLOCK
from exit_workflow.domain.enums import ActorRole
from exit_workflow.domain.errors import (
    AuthorizationError,
    UndefinedErrorCode,
    WorkflowNotFound,
)
from exit_workflow.domain.money import MoneyError, to_minor
from exit_workflow.domain.principal import Principal
from exit_workflow.domain.states import State
from exit_workflow.repositories.workflows import WorkflowRepository
from exit_workflow.services.transitions import apply_transition

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InspectionReportCommand:
    """api.yaml POST /exit-workflows/{id}/inspection-report request body."""

    damage_amount: Decimal
    photos: tuple[str, ...]


class InspectionService:
    def __init__(self, session: AsyncSession, *, clock: Clock = DEFAULT_CLOCK) -> None:
        self._session = session
        self._clock = clock

    async def _load(self, workflow_id: str) -> ExitWorkflow:
        workflow = await WorkflowRepository(self._session).get(workflow_id, for_update=True)
        if workflow is None:
            raise WorkflowNotFound("Exit workflow not found.")
        return workflow

    async def schedule_inspection(self, workflow_id: str, actor: Principal) -> ExitWorkflow:
        """OWNER_NOTIFIED -> INSPECTION_SCHEDULED (states.yaml, rules.yaml#EXIT-05).

        A workflow that has already stalled cannot be scheduled: states.yaml
        gives STALLED no outgoing edge at all, so the state machine refuses it
        and the caller gets 409 WRONG_STATE. What *should* happen to a stalled
        exit is undecided (blockers.md#B-2).
        """
        workflow = await self._load(workflow_id)

        # api.yaml authz: owner.
        if actor.role is not ActorRole.OWNER or workflow.owner_id != actor.uuid:
            raise AuthorizationError("Only the property owner may schedule the inspection.")

        await apply_transition(
            self._session,
            workflow,
            State.INSPECTION_SCHEDULED,
            actor_type=actor.role,
            actor_id=actor.subject_id,
        )
        workflow.inspection_scheduled_at = clock_module.now_utc(self._clock)
        await self._session.flush()
        return workflow

    async def submit_report(
        self, workflow_id: str, command: InspectionReportCommand, actor: Principal
    ) -> ExitWorkflow:
        """INSPECTION_SCHEDULED -> INSPECTION_DONE (algorithm.md step 7)."""
        workflow = await self._load(workflow_id)

        # api.yaml authz: inspection_agency. states.yaml names the same party
        # ``inspector`` on this edge.
        if actor.role is not ActorRole.INSPECTION_AGENCY:
            raise AuthorizationError("Only the inspection agency may submit an inspection report.")

        # rules.yaml#EXIT-06 — "entered by the inspection agency with photos".
        if len(command.photos) < 1:
            raise UndefinedErrorCode(
                "An inspection report requires at least one photo.",
                http_status=422,
                blocker="B-9",
            )

        try:
            damage_minor = to_minor(command.damage_amount)
        except MoneyError as exc:
            raise UndefinedErrorCode(
                f"damage_amount is not a valid AED amount: {exc}",
                http_status=422,
                blocker="B-9",
            ) from exc

        await apply_transition(
            self._session,
            workflow,
            State.INSPECTION_DONE,
            actor_type=actor.role,
            actor_id=actor.subject_id,
            metadata={"damage_amount_minor": damage_minor, "photo_count": len(command.photos)},
        )
        workflow.damage_amount_minor = damage_minor
        workflow.damage_photos = list(command.photos)
        workflow.inspection_reported_at = clock_module.now_utc(self._clock)
        await self._session.flush()
        return workflow
