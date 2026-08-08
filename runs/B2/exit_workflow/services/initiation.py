"""Initiation — algorithm.md steps 1-5.

    1. Load contract, assert ACTIVE, else 422                    (EXIT-01)
    2. Assert no existing workflow, else 409 with existing id    (EXIT-01, X-001)
    3. Validate move_out_date / reason / documents               (EXIT-02, X-007)
    4. ONE TRANSACTION: insert workflow (INITIATED->DOCS_SUBMITTED),
       property.exitLock = true, audit row                       (EXIT-03)
    5. AFTER COMMIT: emit owner notification                     (EXIT-04, X-002)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..clock import Clock
from ..db.models import (
    CONTRACT_STATUS_ACTIVE,
    Contract,
    ExitWorkflow,
    Property,
    workflow_id_seq,
)
from ..db.session import transaction
from ..domain.rules import (
    format_workflow_id,
    validate_documents,
    validate_move_out_date,
    validate_reason,
)
from ..domain.states import Actor, State
from ..errors import ContractNotActive, ExitAlreadyInProgress, NotAuthorized
from ..ports import ExitReasonReference
from .notification import NotificationService
from .transitions import Principal, TransitionService


@dataclass(frozen=True, slots=True)
class InitiationCommand:
    contract_id: uuid.UUID
    move_out_date: date
    reason: str
    documents: list[dict[str, Any]]


class InitiationService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        transitions: TransitionService,
        notifications: NotificationService,
        reason_reference: ExitReasonReference,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._transitions = transitions
        self._notifications = notifications
        self._reason_reference = reason_reference

    async def initiate(self, command: InitiationCommand, actor: Principal) -> ExitWorkflow:
        # api.yaml /exit-workflows post authz: "tenant, own active contract only".
        if actor.role is not Actor.TENANT:
            raise NotAuthorized("only a tenant may initiate an exit workflow")

        # Reference data is fetched outside the transaction; it is remote data,
        # not part of the atomic unit rules.yaml#EXIT-03 describes.
        allowed_reasons = await self._reason_reference.exit_reasons()

        try:
            async with transaction(self._session_factory) as session:
                workflow = await self._initiate_in_transaction(
                    session, command, actor, allowed_reasons
                )
                # Written inside the same transaction so the event cannot be lost;
                # dispatched only after commit (rules.yaml#EXIT-04).
                event_id = await self._notifications.enqueue_owner_notification(session, workflow)
        except IntegrityError as exc:
            # edges.yaml#X-001 — the partial unique index is the real guard against a
            # concurrent duplicate initiation. Never a second workflow.
            if _is_open_workflow_conflict(exc):
                raise await self._existing_workflow_conflict(command.contract_id) from exc
            raise

        # algorithm.md step 5 — AFTER COMMIT. A dispatch failure never rolls the
        # workflow back; it retries with backoff and then dead-letters
        # (rules.yaml#EXIT-04, edges.yaml#X-002).
        await self._notifications.dispatch_now(event_id)

        return workflow

    async def _initiate_in_transaction(
        self,
        session: AsyncSession,
        command: InitiationCommand,
        actor: Principal,
        allowed_reasons,
    ) -> ExitWorkflow:
        # --- step 1: load contract, assert ACTIVE (rules.yaml#EXIT-01) ---------
        contract = (
            await session.execute(
                select(Contract).where(Contract.id == command.contract_id).with_for_update()
            )
        ).scalar_one_or_none()
        if contract is None:
            # An unknown contract is indistinguishable from someone else's
            # contract to a tenant caller; both are refused as not-yours.
            raise NotAuthorized("contract not found or not accessible by this tenant")
        if str(contract.tenant_id) != actor.id:
            # api.yaml authz: "own active contract only".
            raise NotAuthorized("contract does not belong to this tenant")
        if contract.status != CONTRACT_STATUS_ACTIVE:
            # algorithm.md step 1 — 422. api.yaml defines no code for this case
            # (blockers.md#B-004), so the response carries code: null.
            raise ContractNotActive(f"contract status is {contract.status}, expected ACTIVE")

        # --- step 2: no existing workflow (rules.yaml#EXIT-01, edges.yaml#X-001) ---
        existing = (
            await session.execute(
                select(ExitWorkflow.id)
                .where(ExitWorkflow.contract_id == command.contract_id)
                .where(ExitWorkflow.status != State.COMPLETE)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ExitAlreadyInProgress(existing)

        # --- step 3: field validation (rules.yaml#EXIT-02, edges.yaml#X-007) ---
        validate_move_out_date(command.move_out_date, self._clock.today_dubai())
        validate_reason(command.reason, allowed_reasons)
        validate_documents(command.documents)

        # --- step 4: one transaction (rules.yaml#EXIT-03) ----------------------
        property_row = (
            await session.execute(
                select(Property).where(Property.id == contract.property_id).with_for_update()
            )
        ).scalar_one()

        # rules.yaml#EXIT-02 — EX-YYYYMMDD-NNNNN, sequence from PostgreSQL.
        sequence_value = (await session.execute(workflow_id_seq.next_value().select())).scalar_one()
        workflow_id = format_workflow_id(self._clock.today_dubai(), sequence_value)

        workflow = ExitWorkflow(
            id=workflow_id,
            contract_id=contract.id,
            property_id=contract.property_id,
            tenant_id=contract.tenant_id,
            owner_id=property_row.owner_id,
            status=State.INITIATED,  # states.yaml#exit_workflow.initial
            move_out_date=command.move_out_date,
            reason=command.reason,
            documents=command.documents,
            security_deposit_minor=contract.security_deposit_minor,
            created_at=self._clock.now_utc(),
            updated_at=self._clock.now_utc(),
        )
        session.add(workflow)
        self._transitions.record_creation(
            session,
            workflow,
            actor,
            metadata={"contract_id": str(contract.id), "reason": command.reason},
        )

        # algorithm.md step 4 — INITIATED -> DOCS_SUBMITTED inside the same
        # transaction as the insert. states.yaml requires move_out_date, reason
        # and documents for this transition; all three are validated above.
        await self._transitions.apply(
            session,
            workflow,
            State.DOCS_SUBMITTED,
            actor,
            metadata={"documents": len(command.documents)},
        )

        # rules.yaml#EXIT-03 — exitLock true IN THE SAME TRANSACTION. Blocks new
        # contracts on the property (BR-1); released only by COMPLETE.
        property_row.exit_lock = True

        # Surfaces a concurrent duplicate as IntegrityError here rather than at
        # commit; the caller translates it (edges.yaml#X-001).
        await session.flush()
        return workflow

    async def _existing_workflow_conflict(self, contract_id: uuid.UUID) -> ExitAlreadyInProgress:
        """Re-read in a fresh transaction to report the winner's id (X-001)."""
        async with transaction(self._session_factory) as session:
            existing = (
                await session.execute(
                    select(ExitWorkflow.id)
                    .where(ExitWorkflow.contract_id == contract_id)
                    .where(ExitWorkflow.status != State.COMPLETE)
                )
            ).scalar_one_or_none()
        return ExitAlreadyInProgress(existing or "unknown")


def _is_open_workflow_conflict(exc: IntegrityError) -> bool:
    constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
    return constraint == "uq_exit_workflow_open_per_contract" or (
        "uq_exit_workflow_open_per_contract" in str(exc.orig)
    )
