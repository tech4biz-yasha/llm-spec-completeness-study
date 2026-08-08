"""Inspection and damage confirmation — algorithm.md steps 6-8.

    6. Owner schedules inspection                                  (EXIT-05)
    7. Agency uploads damage_amount + photos -> INSPECTION_DONE
    8. Owner confirms -> DAMAGE_CONFIRMED. Owner may dispute once
       -> admin review                                             (EXIT-06)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..clock import Clock
from ..db.models import ExitWorkflow
from ..db.session import transaction
from ..domain.states import Actor, State
from ..errors import NotAuthorized, SpecUnresolved, WorkflowNotFound
from ..money import to_minor
from .transitions import Principal, TransitionService


@dataclass(frozen=True, slots=True)
class InspectionReport:
    damage_amount: Decimal
    photos: list[dict[str, Any]]


class InspectionService:
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

    async def schedule_inspection(
        self, workflow_id: str, actor: Principal, *, scheduled_for: date | None = None
    ) -> ExitWorkflow:
        """api.yaml /schedule-inspection, authz owner. states.yaml
        OWNER_NOTIFIED -> INSPECTION_SCHEDULED (rules.yaml#EXIT-05)."""
        async with transaction(self._session_factory) as session:
            workflow = await self._load_for_update(session, workflow_id)
            self._require_owner(workflow, actor)
            workflow.inspection_scheduled_at = self._clock.now_utc()
            workflow.inspection_scheduled_for = scheduled_for
            await self._transitions.apply(
                session,
                workflow,
                State.INSPECTION_SCHEDULED,
                actor,
                metadata={
                    "scheduled_for": scheduled_for.isoformat() if scheduled_for else None,
                    # rules.yaml#EXIT-05 — the 30-day window is measured from move_out_date.
                    "move_out_date": workflow.move_out_date.isoformat(),
                },
            )
            return workflow

    async def submit_report(
        self, workflow_id: str, report: InspectionReport, actor: Principal
    ) -> ExitWorkflow:
        """api.yaml /inspection-report, authz inspection_agency. states.yaml
        INSPECTION_SCHEDULED -> INSPECTION_DONE (rules.yaml#EXIT-06).

        blockers.md#B-012: nothing in the spec ties a particular inspection
        agency to a workflow, so any principal holding the inspection_agency
        role is accepted. That is the spec as written, not a chosen policy.
        """
        if actor.role not in (Actor.INSPECTION_AGENCY, Actor.INSPECTOR):
            raise NotAuthorized("only the inspection agency may submit an inspection report")

        async with transaction(self._session_factory) as session:
            workflow = await self._load_for_update(session, workflow_id)
            # rules.yaml#EXIT-06 — damage assessment is entered "with photos".
            workflow.damage_amount_minor = to_minor(report.damage_amount)
            workflow.inspection_photos = report.photos
            workflow.inspection_reported_at = self._clock.now_utc()
            await self._transitions.apply(
                session,
                workflow,
                State.INSPECTION_DONE,
                actor,
                metadata={
                    "damage_amount_minor": workflow.damage_amount_minor,
                    "photo_count": len(report.photos),
                },
            )
            return workflow

    async def confirm_damage(self, workflow_id: str, actor: Principal) -> ExitWorkflow:
        """api.yaml /confirm-damage, authz owner. states.yaml
        INSPECTION_DONE -> DAMAGE_CONFIRMED.

        rules.yaml#EXIT-06 — "Owner confirmation is required before settlement";
        states.yaml forbids INSPECTION_DONE -> REFUND_PROCESSED for that reason.
        The confirmed figure is the agency's reported figure; the spec gives the
        owner a confirm/dispute decision, not an amount to edit.
        """
        async with transaction(self._session_factory) as session:
            workflow = await self._load_for_update(session, workflow_id)
            self._require_owner(workflow, actor)
            if workflow.status is State.INSPECTION_DONE and workflow.damage_amount_minor is None:
                # Unreachable through the state machine (INSPECTION_DONE always
                # carries a reported amount); kept as a hard invariant. Any other
                # source state is rejected by apply() below as WRONG_STATE.
                raise ValueError("no inspection report on workflow in INSPECTION_DONE")
            workflow.confirmed_damage_minor = workflow.damage_amount_minor
            workflow.damage_confirmed_at = self._clock.now_utc()
            await self._transitions.apply(
                session,
                workflow,
                State.DAMAGE_CONFIRMED,
                actor,
                metadata={"confirmed_damage_minor": workflow.confirmed_damage_minor},
            )
            return workflow

    async def dispute_damage(self, workflow_id: str, actor: Principal) -> ExitWorkflow:
        """rules.yaml#EXIT-06 — "Owner may dispute once; a dispute routes to
        admin review."

        BLOCKED (blockers.md#B-002). states.yaml declares no state for a dispute
        or for admin review, no transition out of INSPECTION_DONE other than
        DAMAGE_CONFIRMED, and api.yaml exposes no dispute endpoint. Where the
        dispute leaves the workflow, and what an admin decision does to the
        confirmed damage figure, are unanswered. Raising rather than inventing a
        state (AGENTS.md).
        """
        raise SpecUnresolved(
            "B-002",
            "owner damage dispute (rules.yaml#EXIT-06) has no state, transition or "
            "endpoint in states.yaml / api.yaml",
            details={"workflow_id": workflow_id},
        )

    # -- helpers -----------------------------------------------------------

    async def _load_for_update(self, session: AsyncSession, workflow_id: str) -> ExitWorkflow:
        workflow = (
            await session.execute(
                select(ExitWorkflow).where(ExitWorkflow.id == workflow_id).with_for_update()
            )
        ).scalar_one_or_none()
        if workflow is None:
            raise WorkflowNotFound(f"no exit workflow {workflow_id}")
        return workflow

    @staticmethod
    def _require_owner(workflow: ExitWorkflow, actor: Principal) -> None:
        # api.yaml authz: owner. The owner of record is the property owner
        # captured at initiation.
        if actor.role is not Actor.OWNER:
            raise NotAuthorized("only the owner may perform this step")
        if str(workflow.owner_id) != actor.id:
            raise NotAuthorized("this workflow belongs to another owner")
