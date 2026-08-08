"""Settlement — algorithm.md steps 9 to 13.

     9. BRANCH on confirmed_damage vs security_deposit:
        - confirmed_damage <= deposit -> step 10
        - confirmed_damage >  deposit -> raise SpecUnresolved("R8"). STOP. (EXIT-07, X-003)
    10. refund = deposit - confirmed_damage, Decimal, half-up 2dp. Create payment
        type DEPOSIT_REFUND, idempotency_key = workflow_id. (EXIT-07, X-005)
    11. Await gateway SUCCEEDED. PENDING or FAILED -> hold, never proceed. (EXIT-08, X-004)
    12. Generate NOC PDF, store UAE bucket, immutable, link to workflow. (EXIT-09)
    13. IN ONE TRANSACTION: status = COMPLETE, release property.exitLock, audit row. (EXIT-09)

:meth:`SettlementService.settle` drives as far through these steps as the
gateway permits and is safe to call again: a workflow held at step 11 by a
PENDING payment resumes from where it stopped when the caller retries. The
workflow row is taken ``FOR UPDATE`` on entry, which is what makes two
concurrent calls (edges.yaml#X-005) resolve to one payment and one NOC.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import ExitWorkflow, Payment
from exit_workflow.domain import clock as clock_module
from exit_workflow.domain.clock import Clock, DEFAULT_CLOCK
from exit_workflow.domain.enums import ActorRole, PaymentStatus, PaymentType
from exit_workflow.domain.errors import (
    AuthorizationError,
    PaymentPending,
    SpecUnresolved,
    UndefinedErrorCode,
    WorkflowNotFound,
    WrongState,
)
from exit_workflow.domain.money import from_minor
from exit_workflow.domain.principal import Principal
from exit_workflow.domain.states import State
from exit_workflow.gateway.payments import GatewayError, PaymentGateway
from exit_workflow.repositories.payments import PaymentRepository
from exit_workflow.repositories.properties import PropertyRepository
from exit_workflow.repositories.workflows import WorkflowRepository
from exit_workflow.services.noc import NocIssuanceService
from exit_workflow.services.transitions import apply_transition
from exit_workflow.storage.noc import NocStorage

logger = logging.getLogger(__name__)

#: The states from which /settle can make progress. Anything earlier is a
#: caller error (409 WRONG_STATE); COMPLETE is answered idempotently.
SETTLEABLE_STATES = (State.DAMAGE_CONFIRMED, State.REFUND_PROCESSED, State.NOC_ISSUED)


@dataclass(frozen=True, slots=True)
class SettlementResult:
    """api.yaml 200 body: ``{refund_amount, payment_id, status}``."""

    refund_amount: Decimal
    payment_id: str
    status: State


class SettlementService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        gateway: PaymentGateway,
        noc_storage: NocStorage,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        self._session = session
        self._gateway = gateway
        self._noc_storage = noc_storage
        self._clock = clock

    async def settle(self, workflow_id: str, actor: Principal) -> SettlementResult:
        workflow = await WorkflowRepository(self._session).get(workflow_id, for_update=True)
        if workflow is None:
            raise WorkflowNotFound("Exit workflow not found.")

        # api.yaml authz: system|owner.
        if actor.role is ActorRole.OWNER:
            if workflow.owner_id != actor.uuid:
                raise AuthorizationError("Only the property owner may settle this workflow.")
        elif actor.role is not ActorRole.SYSTEM:
            raise AuthorizationError("Only the system or the property owner may settle an exit.")

        status = State(workflow.status)

        if status is State.COMPLETE:
            # Already settled; report the stored outcome rather than repeating
            # any of it (edges.yaml#X-005).
            return self._result(workflow, status)

        if status not in SETTLEABLE_STATES:
            raise WrongState(
                "Settlement requires owner-confirmed damages.",
                current=str(status),
                expected=[str(state) for state in SETTLEABLE_STATES],
            )

        if status is State.DAMAGE_CONFIRMED:
            status = await self._process_refund(workflow, actor)

        if status is State.REFUND_PROCESSED:
            status = await self._issue_noc(workflow, actor)

        if status is State.NOC_ISSUED:
            status = await self._complete(workflow, actor)

        await self._session.flush()
        return self._result(workflow, status)

    # --- step 9 to 11 ---------------------------------------------------------

    async def _process_refund(self, workflow: ExitWorkflow, actor: Principal) -> State:
        """Steps 9, 10 and 11."""
        if workflow.damage_amount_minor is None:  # pragma: no cover - guarded at confirm-damage
            raise WrongState(
                "No confirmed damage amount is recorded for this workflow.",
                current=workflow.status,
            )

        deposit_minor = workflow.security_deposit_minor
        damage_minor = workflow.damage_amount_minor

        # algorithm.md step 9 / rules.yaml#EXIT-07, edges.yaml#X-003.
        if damage_minor > deposit_minor:
            # risks.md#R8 lists three viable readings — write off, raise a debt,
            # or block the exit — and the client has not picked one. No refund,
            # no NOC; the workflow holds at DAMAGE_CONFIRMED.
            raise SpecUnresolved(
                "R8",
                "Confirmed damage exceeds the security deposit. The behaviour for this case "
                "is undecided (risks.md#R8); settlement cannot proceed.",
                confirmed_damage=str(from_minor(damage_minor)),
                security_deposit=str(from_minor(deposit_minor)),
            )

        # algorithm.md step 10 / rules.yaml#EXIT-07:
        # refund = max(security_deposit - confirmed_damage, 0), Decimal, half-up 2 dp.
        # Both operands are exact integer fils, so the subtraction is exact and
        # the half-up rounding named by the rule has nothing left to round. The
        # max() is what the rule says; the damage > deposit branch above means
        # it can never actually clamp.
        refund_minor = max(deposit_minor - damage_minor, 0)
        workflow.refund_amount_minor = refund_minor

        payment, created = await PaymentRepository(self._session).create_or_get(
            # rules.yaml#EXIT-08 / edges.yaml#X-005 — idempotency key = workflow ID.
            idempotency_key=workflow.id,
            payment_type=PaymentType.DEPOSIT_REFUND,
            workflow_id=workflow.id,
            contract_id=workflow.contract_id,
            payee_id=workflow.tenant_id,
            amount_minor=refund_minor,
        )
        workflow.payment_id = payment.id

        if created:
            await self._submit_to_gateway(payment)
        elif payment.status == str(PaymentStatus.PENDING):
            await self._refresh_from_gateway(payment)

        # algorithm.md step 11 / rules.yaml#EXIT-08, edges.yaml#X-004.
        self._require_succeeded(payment)

        await apply_transition(
            self._session,
            workflow,
            State.REFUND_PROCESSED,
            actor_type=actor.role,
            actor_id=actor.subject_id,
            metadata={
                "refund_amount_minor": refund_minor,
                "security_deposit_minor": deposit_minor,
                "confirmed_damage_minor": damage_minor,
                "payment_id": str(payment.id),
            },
        )
        return State.REFUND_PROCESSED

    async def _submit_to_gateway(self, payment: Payment) -> None:
        try:
            result = await self._gateway.submit_refund(
                payment_id=payment.id,
                idempotency_key=payment.idempotency_key,
                amount_minor=payment.amount_minor,
                currency=payment.currency,
                payee_id=payment.payee_id,
            )
        except GatewayError as exc:
            # The payment row stays PENDING and the workflow stays at
            # DAMAGE_CONFIRMED: "hold, never proceed" (algorithm.md step 11).
            logger.error("refund submission failed for payment %s: %s", payment.id, exc)
            raise PaymentPending(
                "The refund could not be submitted to the payment gateway; the workflow is "
                "held until the refund succeeds.",
                details={"payment_id": str(payment.id)},
            ) from exc
        self._apply_gateway_result(payment, result)

    async def _refresh_from_gateway(self, payment: Payment) -> None:
        try:
            result = await self._gateway.fetch_status(
                idempotency_key=payment.idempotency_key, reference=payment.gateway_reference
            )
        except GatewayError as exc:
            logger.error("refund status check failed for payment %s: %s", payment.id, exc)
            raise PaymentPending(
                "The refund status could not be read from the payment gateway.",
                details={"payment_id": str(payment.id)},
            ) from exc
        self._apply_gateway_result(payment, result)

    def _apply_gateway_result(self, payment: Payment, result) -> None:
        payment.status = str(result.status)
        payment.gateway_reference = result.reference or payment.gateway_reference
        payment.gateway_status_checked_at = clock_module.now_utc(self._clock)
        if result.status is PaymentStatus.SUCCEEDED:
            payment.settled_at = clock_module.now_utc(self._clock)
            payment.failure_reason = None
        elif result.status is PaymentStatus.FAILED:
            payment.failure_reason = result.failure_reason

    def _require_succeeded(self, payment: Payment) -> None:
        """algorithm.md step 11 — PENDING or FAILED hold, never proceed."""
        if payment.status == str(PaymentStatus.SUCCEEDED):
            return

        if payment.status == str(PaymentStatus.PENDING):
            raise PaymentPending(
                "The deposit refund has not been confirmed by the gateway; the NOC cannot be "
                "issued until it succeeds.",
                details={"payment_id": str(payment.id), "payment_status": payment.status},
            )

        # FAILED. algorithm.md gives the behaviour ("hold, never proceed") but
        # api.yaml defines no code for it, and neither the kit's retry policy nor
        # its escalation path for a failed refund exists (blockers.md#B-6).
        raise UndefinedErrorCode(
            "The deposit refund failed at the gateway; the workflow is held and no NOC is "
            "issued. Recovery for a failed refund is not specified.",
            http_status=409,
            blocker="B-6",
            # Keep the failed payment record: it is the evidence of what the
            # gateway was asked to do.
            preserves_transaction=True,
            payment_id=str(payment.id),
            payment_status=payment.status,
            failure_reason=payment.failure_reason,
        )

    # --- step 12 --------------------------------------------------------------

    async def _issue_noc(self, workflow: ExitWorkflow, actor: Principal) -> State:
        """Step 12, after re-checking the payment (edges.yaml#X-004)."""
        payment = await self._session.get(Payment, workflow.payment_id)
        if payment is None:  # pragma: no cover - payment_id is a foreign key
            raise WrongState(
                "The workflow is REFUND_PROCESSED but no refund payment is linked.",
                current=workflow.status,
            )
        # X-004: "Refund payment still PENDING, NOC generation attempted -> Refuse."
        # Re-read rather than trusting the earlier branch: this method is also
        # the entry point when a caller resumes a held workflow.
        if payment.status == str(PaymentStatus.PENDING):
            await self._refresh_from_gateway(payment)
        self._require_succeeded(payment)

        document = await NocIssuanceService(
            self._session, self._noc_storage, clock=self._clock
        ).issue(workflow)

        # states.yaml forbids "any -> NOC_ISSUED without REFUND_PROCESSED"
        # (T13 order, rules.yaml#EXIT-08); the state machine enforces it here.
        await apply_transition(
            self._session,
            workflow,
            State.NOC_ISSUED,
            actor_type=actor.role,
            actor_id=actor.subject_id,
            metadata={
                "noc_document_id": str(document.id),
                "noc_object_key": document.object_key,
                "noc_sha256": document.content_sha256,
            },
        )
        return State.NOC_ISSUED

    # --- step 13 --------------------------------------------------------------

    async def _complete(self, workflow: ExitWorkflow, actor: Principal) -> State:
        """Step 13 — COMPLETE, exit lock released, audit row, one transaction.

        All three happen in the caller's transaction, which is the same one that
        issued the NOC, so the lock cannot be released without the completion
        being recorded (rules.yaml#EXIT-09).
        """
        await apply_transition(
            self._session,
            workflow,
            State.COMPLETE,
            actor_type=actor.role,
            actor_id=actor.subject_id,
            metadata={"exit_lock_released": True},
        )
        await PropertyRepository(self._session).release_exit_lock(workflow.property_id, workflow.id)
        workflow.completed_at = clock_module.now_utc(self._clock)
        return State.COMPLETE

    @staticmethod
    def _result(workflow: ExitWorkflow, status: State) -> SettlementResult:
        return SettlementResult(
            refund_amount=from_minor(workflow.refund_amount_minor or 0),
            payment_id=str(workflow.payment_id),
            status=status,
        )
