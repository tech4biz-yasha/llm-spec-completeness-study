"""Deposit settlement — algorithm.md steps 9-11, then hand off to NOC.

     9. BRANCH on confirmed_damage vs security_deposit:
        - confirmed_damage <= deposit -> step 10
        - confirmed_damage >  deposit -> raise SpecUnresolved("R8"). STOP.
                                                        (EXIT-07, X-003)
    10. refund = deposit - confirmed_damage, Decimal, half-up 2dp. Create
        payment type DEPOSIT_REFUND, idempotency_key = workflow_id.
                                                        (EXIT-07, X-005)
    11. Await gateway SUCCEEDED. PENDING or FAILED -> hold, never proceed.
                                                        (EXIT-08, X-004)
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..clock import Clock
from ..db.models import (
    ExitWorkflow,
    Payment,
    PaymentStatus,
    PaymentType,
)
from ..db.session import transaction
from ..domain.rules import refund_minor
from ..domain.states import Actor, State
from ..errors import NotAuthorized, PaymentPending, WorkflowNotFound, WrongState
from ..money import CURRENCY, from_minor
from ..ports import PaymentGateway, RefundRequest
from .noc import NocService
from .transitions import SYSTEM_PRINCIPAL, Principal, TransitionService

logger = logging.getLogger(__name__)

#: Gateway statuses this module understands (algorithm.md step 11).
_SUCCEEDED = "SUCCEEDED"
_PENDING = "PENDING"
_FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """api.yaml /settle 200: {refund_amount, payment_id, status}."""

    workflow_id: str
    refund_amount_minor: int
    payment_id: uuid.UUID
    status: State


class SettlementService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        transitions: TransitionService,
        gateway: PaymentGateway,
        noc: NocService,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._transitions = transitions
        self._gateway = gateway
        self._noc = noc

    async def settle(self, workflow_id: str, actor: Principal) -> SettlementResult:
        # api.yaml /settle authz: system|owner.
        if actor.role not in (Actor.SYSTEM, Actor.OWNER):
            raise NotAuthorized("only the system or the owner may trigger settlement")

        payment_id, refund_amount_minor, already_settled = await self._ensure_payment(
            workflow_id, actor
        )

        if not already_settled:
            # rules.yaml#EXIT-08 — DEPOSIT_REFUND through the gateway, keyed by
            # the workflow id so a retry or a concurrent call cannot double-pay
            # (edges.yaml#X-005).
            await self._drive_gateway(workflow_id, payment_id)

        status = await self._advance_after_payment(workflow_id, actor)

        return SettlementResult(
            workflow_id=workflow_id,
            refund_amount_minor=refund_amount_minor,
            payment_id=payment_id,
            status=status,
        )

    # -- step 9 + 10 -------------------------------------------------------

    async def _ensure_payment(
        self, workflow_id: str, actor: Principal
    ) -> tuple[uuid.UUID, int, bool]:
        """Compute the refund and get-or-create the DEPOSIT_REFUND payment.

        Returns (payment_id, refund_amount_minor, already_settled). The third
        value is True when the workflow has already moved past REFUND_PROCESSED,
        in which case the gateway is not called again.
        """
        async with transaction(self._session_factory) as session:
            workflow = (
                await session.execute(
                    select(ExitWorkflow).where(ExitWorkflow.id == workflow_id).with_for_update()
                )
            ).scalar_one_or_none()
            if workflow is None:
                raise WorkflowNotFound(f"no exit workflow {workflow_id}")

            # api.yaml authz: system|owner — the owner of *this* workflow.
            if actor.role is Actor.OWNER and str(workflow.owner_id) != actor.id:
                raise NotAuthorized("this workflow belongs to another owner")

            existing = (
                await session.execute(
                    select(Payment).where(Payment.idempotency_key == workflow.id)
                )
            ).scalar_one_or_none()

            if workflow.status in (
                State.REFUND_PROCESSED,
                State.NOC_ISSUED,
                State.COMPLETE,
            ):
                # edges.yaml#X-005 — a second call returns the existing payment.
                if existing is None:  # pragma: no cover — invariant
                    raise ValueError(f"{workflow.id} is {workflow.status} with no payment row")
                return existing.id, workflow.refund_amount_minor or 0, True

            if workflow.status is not State.DAMAGE_CONFIRMED:
                # states.yaml forbids DOCS_SUBMITTED -> REFUND_PROCESSED and
                # INSPECTION_DONE -> REFUND_PROCESSED (owner confirmation is
                # mandatory, rules.yaml#EXIT-06).
                raise WrongState(
                    f"settlement requires DAMAGE_CONFIRMED, workflow is {workflow.status}",
                    from_state=workflow.status,
                    to_state=State.REFUND_PROCESSED,
                )

            if workflow.confirmed_damage_minor is None:  # pragma: no cover — invariant
                raise ValueError(f"{workflow.id} is DAMAGE_CONFIRMED with no confirmed damage")

            # algorithm.md step 9 — the branch. Raises SpecUnresolved("R8") when
            # confirmed_damage > security_deposit (rules.yaml#EXIT-07,
            # edges.yaml#X-003): no refund, no NOC, workflow holds at
            # DAMAGE_CONFIRMED. Nothing below this line runs in that case.
            amount_minor = refund_minor(
                workflow.security_deposit_minor, workflow.confirmed_damage_minor
            )

            if existing is not None:
                workflow.refund_amount_minor = existing.amount_minor
                return existing.id, existing.amount_minor, False

            # rules.yaml#EXIT-08 — idempotency_key = workflow_id. The unique
            # constraint decides the race; the loser reads the winner's row.
            new_id = uuid.uuid4()
            inserted = (
                await session.execute(
                    pg_insert(Payment)
                    .values(
                        id=new_id,
                        workflow_id=workflow.id,
                        type=PaymentType.DEPOSIT_REFUND.value,
                        amount_minor=amount_minor,
                        currency=CURRENCY,
                        status=PaymentStatus.PENDING.value,
                        idempotency_key=workflow.id,
                        created_at=self._clock.now_utc(),
                        updated_at=self._clock.now_utc(),
                    )
                    .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
                    .returning(Payment.id)
                )
            ).scalar_one_or_none()

            if inserted is None:
                winner = (
                    await session.execute(
                        select(Payment).where(Payment.idempotency_key == workflow.id)
                    )
                ).scalar_one()
                workflow.refund_amount_minor = winner.amount_minor
                return winner.id, winner.amount_minor, False

            workflow.refund_amount_minor = amount_minor
            return new_id, amount_minor, False

    # -- step 11 -----------------------------------------------------------

    async def _drive_gateway(self, workflow_id: str, payment_id: uuid.UUID) -> None:
        """Send the refund (or re-read its status) and record the outcome."""
        async with transaction(self._session_factory) as session:
            payment = (
                await session.execute(
                    select(Payment).where(Payment.id == payment_id).with_for_update()
                )
            ).scalar_one()
            workflow = (
                await session.execute(
                    select(ExitWorkflow).where(ExitWorkflow.id == workflow_id)
                )
            ).scalar_one()
            if payment.status is PaymentStatus.SUCCEEDED:
                return
            request = RefundRequest(
                idempotency_key=payment.idempotency_key,  # rules.yaml#EXIT-08
                workflow_id=workflow.id,
                contract_id=str(workflow.contract_id),
                tenant_id=str(workflow.tenant_id),
                amount=from_minor(payment.amount_minor),
                currency=payment.currency,
            )
            # A gateway reference means this refund was already handed over on an
            # earlier attempt; re-sending it is unnecessary even though the
            # idempotency key would make it safe (rules.yaml#EXIT-08).
            already_sent = payment.gateway_reference is not None

        result = (
            await self._gateway.get_status(request.idempotency_key)
            if already_sent
            else await self._gateway.initiate_refund(request)
        )

        async with transaction(self._session_factory) as session:
            payment = (
                await session.execute(
                    select(Payment).where(Payment.id == payment_id).with_for_update()
                )
            ).scalar_one()
            if result.status == _SUCCEEDED:
                payment.status = PaymentStatus.SUCCEEDED
            elif result.status == _FAILED:
                payment.status = PaymentStatus.FAILED
                payment.failure_reason = result.failure_reason
            elif result.status == _PENDING:
                payment.status = PaymentStatus.PENDING
            else:
                raise ValueError(f"unknown gateway status {result.status!r}")
            payment.gateway_reference = result.reference or payment.gateway_reference
            payment.updated_at = self._clock.now_utc()

    async def _advance_after_payment(self, workflow_id: str, actor: Principal) -> State:
        """DAMAGE_CONFIRMED -> REFUND_PROCESSED once, then NOC and COMPLETE."""
        async with transaction(self._session_factory) as session:
            workflow = (
                await session.execute(
                    select(ExitWorkflow).where(ExitWorkflow.id == workflow_id).with_for_update()
                )
            ).scalar_one()
            payment = (
                await session.execute(
                    select(Payment).where(Payment.idempotency_key == workflow_id).with_for_update()
                )
            ).scalar_one()

            if workflow.status is State.DAMAGE_CONFIRMED:
                # algorithm.md step 11 — PENDING or FAILED: hold, never proceed.
                # The workflow stays at DAMAGE_CONFIRMED and the caller gets 409
                # PAYMENT_PENDING. api.yaml defines no separate code for a FAILED
                # refund (blockers.md#B-005), so both non-SUCCEEDED outcomes
                # report PAYMENT_PENDING; the payment row carries the true status.
                if payment.status is not PaymentStatus.SUCCEEDED:
                    raise PaymentPending(
                        f"refund payment is {payment.status.value}; settlement holds",
                        details={
                            "payment_id": str(payment.id),
                            "payment_status": payment.status.value,
                            "failure_reason": payment.failure_reason,
                        },
                    )
                # states.yaml: DAMAGE_CONFIRMED -> REFUND_PROCESSED, actor system.
                await self._transitions.apply(
                    session,
                    workflow,
                    State.REFUND_PROCESSED,
                    SYSTEM_PRINCIPAL,
                    metadata={
                        "payment_id": str(payment.id),
                        "refund_amount_minor": payment.amount_minor,
                        "gateway_reference": payment.gateway_reference,
                        "triggered_by": f"{actor.role.value}:{actor.id}",
                    },
                )

        # algorithm.md steps 12-13 — refund first, then NOC (T13 order,
        # rules.yaml#EXIT-08; risks.md#R10 resolved in favour of T13).
        workflow = await self._noc.issue_and_complete(workflow_id)
        return workflow.status
