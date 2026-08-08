"""Damage confirmation — algorithm.md step 8.

    8. Owner confirms -> DAMAGE_CONFIRMED. Owner may dispute once -> admin
       review. (EXIT-06)

Only the confirmation half is implemented. The dispute half has no state in
states.yaml, no transition, and no endpoint in api.yaml, so there is nothing to
implement it against; it is recorded as blockers.md#B-5. states.yaml also
forbids INSPECTION_DONE -> REFUND_PROCESSED with the note "owner confirmation is
mandatory", which this step is what satisfies.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import ExitWorkflow
from exit_workflow.domain import clock as clock_module
from exit_workflow.domain.clock import Clock, DEFAULT_CLOCK
from exit_workflow.domain.enums import ActorRole
from exit_workflow.domain.errors import AuthorizationError, WorkflowNotFound, WrongState
from exit_workflow.domain.principal import Principal
from exit_workflow.domain.states import State
from exit_workflow.repositories.workflows import WorkflowRepository
from exit_workflow.services.transitions import apply_transition

logger = logging.getLogger(__name__)


class DamageService:
    def __init__(self, session: AsyncSession, *, clock: Clock = DEFAULT_CLOCK) -> None:
        self._session = session
        self._clock = clock

    async def confirm_damage(self, workflow_id: str, actor: Principal) -> ExitWorkflow:
        """INSPECTION_DONE -> DAMAGE_CONFIRMED (rules.yaml#EXIT-06)."""
        workflow = await WorkflowRepository(self._session).get(workflow_id, for_update=True)
        if workflow is None:
            raise WorkflowNotFound("Exit workflow not found.")

        # api.yaml authz: owner.
        if actor.role is not ActorRole.OWNER or workflow.owner_id != actor.uuid:
            raise AuthorizationError("Only the property owner may confirm the damage assessment.")

        # There is nothing to confirm if the agency has not reported. The state
        # machine would catch the usual case, but a workflow in INSPECTION_DONE
        # without an amount would otherwise reach settlement with no figure.
        if workflow.damage_amount_minor is None:
            raise WrongState(
                "No inspection report has been submitted for this workflow.",
                current=workflow.status,
                expected=str(State.INSPECTION_DONE),
            )

        await apply_transition(
            self._session,
            workflow,
            State.DAMAGE_CONFIRMED,
            actor_type=actor.role,
            actor_id=actor.subject_id,
            metadata={"confirmed_damage_minor": workflow.damage_amount_minor},
        )
        workflow.damage_confirmed_at = clock_module.now_utc(self._clock)
        await self._session.flush()
        return workflow
