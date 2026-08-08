"""Deposit settlement (SRS O16, T13 step 9).

The money path is deliberately split across transactions:

1. ``finalise_settlement`` freezes the arithmetic and moves to SETTLEMENT_PENDING.
2. ``pay_deposit`` validates, records an idempotency key, moves to SETTLEMENT_PROCESSING
   and commits. **No provider call happens inside that transaction** -- holding a row
   lock across a third-party HTTP call is how a payment system ends up with stuck rows.
3. The provider call runs after commit, and its outcome is written in its own
   transaction. If the process dies in between, the settlement sits in PROCESSING with
   no provider reference and the reconciler re-drives it with the *same* idempotency
   key, so the payout can never be duplicated.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.core.errors import (
    AuthorizationError,
    BusinessRuleViolationError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.money import ZERO, quantize, settle, total
from app.domain import events as ev
from app.domain.enums import (
    ActorRole,
    DeductionCategory,
    ExitWorkflowState,
    PayoutMethod,
    SettlementStatus,
)
from app.domain.events import DomainEvent
from app.domain.policies import assert_no_open_disputes, assert_settlement_finalisable
from app.models.exit_workflow import ExitWorkflow
from app.models.inspection import Inspection
from app.models.settlement import Settlement, SettlementDeduction
from app.ports.notifications import NotificationTemplate
from app.repositories.support import InspectionRepository, SettlementRepository
from app.schemas.settlement import (
    DeductionLineResponse,
    FinaliseSettlementRequest,
    PayDepositRequest,
    SettlementPreview,
)
from app.services.notifications import (
    NotificationService,
    base_context,
    owner_recipient,
    tenant_recipient,
)
from app.services.unit_of_work import UnitOfWork
from app.services.workflow_engine import WorkflowEngine


class PayoutDispatcher(Protocol):
    """Runs the provider call after the transaction commits."""

    async def dispatch(self, workflow_id: uuid.UUID, request_id: str | None) -> None: ...


class NocIssuer(Protocol):
    """Issues the Exit NOC once the refund is confirmed (SRS O16)."""

    async def issue(self, workflow: ExitWorkflow, *, ctx: RequestContext) -> object: ...


class SettlementService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        engine: WorkflowEngine,
        notifications: NotificationService,
        uow: UnitOfWork,
        noc_issuer: NocIssuer,
        payout_dispatcher: PayoutDispatcher | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._engine = engine
        self._notifications = notifications
        self._uow = uow
        self._noc_issuer = noc_issuer
        self._dispatcher = payout_dispatcher
        self._repo = SettlementRepository(session)
        self._inspections = InspectionRepository(session)

    # ------------------------------------------------------------ preview
    async def preview(self, workflow: ExitWorkflow) -> SettlementPreview:
        """Live projection of "deposit minus damage" while review is open."""
        inspection = await self._inspections.get_for_workflow(workflow.id)
        lines = self._lines_from_inspection(inspection)
        deductions = total(line.amount for line in lines)
        net_refund, liability = settle(workflow.security_deposit_amount, deductions)
        open_disputes = (
            await self._inspections.count_open_disputes(inspection.id) if inspection else 0
        )

        blocked: str | None = None
        if workflow.state is not ExitWorkflowState.DAMAGE_REVIEW:
            blocked = (
                "The settlement can only be finalised while the damage review is open "
                f"(current status: {workflow.state.value})."
            )
        elif open_disputes:
            blocked = (
                f"{open_disputes} disputed damage item(s) must be resolved before the "
                "settlement can be finalised."
            )
        elif inspection is None or inspection.reported_at is None:
            blocked = "The inspection report has not been submitted yet."

        return SettlementPreview(
            currency=workflow.currency,
            deposit_amount=workflow.security_deposit_amount,
            total_deductions=deductions,
            net_refund_amount=net_refund,
            tenant_liability_amount=liability,
            open_disputes=open_disputes,
            can_finalise=blocked is None,
            blocked_reason=blocked,
            lines=lines,
        )

    async def get(self, workflow: ExitWorkflow) -> Settlement:
        return await self._repo.require_for_workflow(workflow.id)

    # ----------------------------------------------------------- finalise
    async def finalise(
        self, workflow: ExitWorkflow, request: FinaliseSettlementRequest, ctx: RequestContext
    ) -> Settlement:
        """Freeze the deduction set and move to SETTLEMENT_PENDING."""
        if ctx.principal.role not in (ActorRole.OWNER, ActorRole.ADMIN):
            raise AuthorizationError("Only the owner may finalise the settlement.")
        self._engine.authorise_party(workflow, ctx, action="finalise_settlement")

        inspection = await self._inspections.get_for_workflow(workflow.id)
        if inspection is None or inspection.reported_at is None:
            raise BusinessRuleViolationError(
                rule="INSPECTION_REPORT_REQUIRED",
                message=(
                    "An inspection report must be on file before the settlement is "
                    "finalised."
                ),
            )
        assert_no_open_disputes(await self._inspections.count_open_disputes(inspection.id))

        lines = self._lines_from_inspection(inspection)
        for manual in request.manual_deductions:
            lines.append(
                DeductionLineResponse(
                    damage_item_id=None,
                    category=manual.category,
                    description=manual.description,
                    amount=quantize(manual.amount),
                )
            )

        deductions = total(line.amount for line in lines)
        assert_settlement_finalisable(
            deposit=workflow.security_deposit_amount,
            total_deductions=deductions,
            has_inspection_report=True,
        )
        net_refund, liability = settle(workflow.security_deposit_amount, deductions)

        if request.expected_net_refund is not None and quantize(
            request.expected_net_refund
        ) != net_refund:
            raise ConflictError(
                "The settlement figures have changed since they were displayed. "
                "Review the updated amounts and try again.",
                code="settlement_figures_changed",
                details={
                    "expected_net_refund": str(quantize(request.expected_net_refund)),
                    "actual_net_refund": str(net_refund),
                },
            )

        now = self._clock.now()
        settlement = await self._repo.get_for_workflow(workflow.id)
        if settlement is None:
            settlement = Settlement(
                id=uuid.uuid4(),
                workflow_id=workflow.id,
                currency=workflow.currency,
                deposit_amount=workflow.security_deposit_amount,
            )
            self._repo.add(settlement)
        elif settlement.status not in (
            SettlementStatus.DRAFT,
            SettlementStatus.PENDING_APPROVAL,
            SettlementStatus.FAILED,
        ):
            raise ConflictError(
                "This settlement can no longer be changed.",
                code="settlement_locked",
                details={"status": settlement.status.value},
            )

        settlement.deductions.clear()
        for line in lines:
            settlement.deductions.append(
                SettlementDeduction(
                    id=uuid.uuid4(),
                    damage_item_id=line.damage_item_id,
                    category=line.category,
                    description=line.description,
                    amount=line.amount,
                    created_by=ctx.principal.actor_id,
                )
            )

        settlement.currency = workflow.currency
        settlement.deposit_amount = workflow.security_deposit_amount
        settlement.total_deductions = deductions
        settlement.net_refund_amount = net_refund
        settlement.tenant_liability_amount = liability
        settlement.status = SettlementStatus.PENDING_APPROVAL
        settlement.computed_at = now
        settlement.finalised_at = now
        settlement.finalised_by = ctx.principal.actor_id
        settlement.owner_note = request.owner_note

        workflow.total_deductions = deductions
        workflow.net_refund_amount = net_refund
        workflow.tenant_liability_amount = liability

        self._engine.transition(
            workflow,
            action="finalise_settlement",
            ctx=ctx,
            note=request.owner_note,
            event_type=ev.SETTLEMENT_FINALISED,
            event_payload={
                "settlement_id": str(settlement.id),
                "deposit_amount": str(settlement.deposit_amount),
                "total_deductions": str(deductions),
                "net_refund_amount": str(net_refund),
                "tenant_liability_amount": str(liability),
                "deduction_count": len(lines),
            },
        )
        await self._session.flush()

        self._notifications.enqueue(
            template=NotificationTemplate.OWNER_SETTLEMENT_READY,
            recipients=(owner_recipient(workflow), tenant_recipient(workflow)),
            context={
                **base_context(workflow),
                "deposit_amount": str(settlement.deposit_amount),
                "total_deductions": str(deductions),
                "net_refund_amount": str(net_refund),
                "currency": settlement.currency,
            },
            dedupe_key=f"{settlement.id}:finalised:{now.isoformat()}",
        )
        return settlement

    async def reopen_review(
        self, workflow: ExitWorkflow, reason: str, ctx: RequestContext
    ) -> Settlement:
        """Owner pulls a pending settlement back into damage review."""
        if ctx.principal.role not in (ActorRole.OWNER, ActorRole.ADMIN):
            raise AuthorizationError("Only the owner may reopen the damage review.")
        self._engine.authorise_party(workflow, ctx, action="reopen_damage_review")

        settlement = await self._repo.require_for_workflow(workflow.id)
        if settlement.status is not SettlementStatus.PENDING_APPROVAL:
            raise ConflictError(
                "Only a settlement awaiting payment can be reopened.",
                code="settlement_not_pending",
                details={"status": settlement.status.value},
            )
        settlement.status = SettlementStatus.DRAFT
        settlement.finalised_at = None
        settlement.finalised_by = None

        self._engine.transition(
            workflow, action="reopen_damage_review", ctx=ctx, note=reason
        )
        return settlement

    # -------------------------------------------- O16: owner 'Pay Deposit'
    async def pay_deposit(
        self,
        workflow: ExitWorkflow,
        request: PayDepositRequest,
        ctx: RequestContext,
        *,
        idempotency_key: str | None = None,
    ) -> Settlement:
        """SRS O16: the owner releases the refund (deposit minus damage)."""
        if ctx.principal.role not in (ActorRole.OWNER, ActorRole.ADMIN):
            raise AuthorizationError("Only the owner may release the deposit.")
        self._engine.authorise_party(workflow, ctx, action="pay_deposit")

        settlement = await self._repo.get_for_workflow_for_update(workflow.id)
        if settlement is None:
            raise NotFoundError("The settlement has not been finalised yet.")
        if settlement.status is not SettlementStatus.PENDING_APPROVAL:
            raise ConflictError(
                "This settlement is not awaiting payment.",
                code="settlement_not_payable",
                details={"status": settlement.status.value},
            )

        requires_payout = settlement.net_refund_amount > ZERO
        if requires_payout and request.payout_method is PayoutMethod.OFFSET_ONLY:
            raise ValidationFailedError(
                "OFFSET_ONLY may only be used when the net refund is zero.",
                details={"net_refund_amount": str(settlement.net_refund_amount)},
            )
        if not requires_payout and request.payout_method is not PayoutMethod.OFFSET_ONLY:
            raise ValidationFailedError(
                "The net refund is zero; use payout_method=OFFSET_ONLY to close the "
                "settlement without a transfer.",
                details={"net_refund_amount": str(settlement.net_refund_amount)},
            )

        now = self._clock.now()
        settlement.payout_method = request.payout_method
        settlement.payout_account_ref = request.payout_account_ref
        settlement.payout_account_name = request.payout_account_name
        settlement.payout_account_last4 = request.payout_account_last4
        settlement.payment_initiated_at = now
        settlement.payment_initiated_by = ctx.principal.actor_id
        settlement.payment_attempts += 1
        settlement.failure_code = None
        settlement.failure_reason = None
        if settlement.payment_idempotency_key is None:
            # Bound to the settlement, not to the click: every retry of this payout --
            # by the owner, by the reconciler -- reuses the same key at the provider.
            settlement.payment_idempotency_key = (
                idempotency_key or f"exit-settlement-{settlement.id}"
            )

        self._engine.transition(
            workflow,
            action="pay_deposit",
            ctx=ctx,
            note=request.note,
            event_type=ev.SETTLEMENT_PAYMENT_INITIATED,
            event_payload={
                "settlement_id": str(settlement.id),
                "net_refund_amount": str(settlement.net_refund_amount),
                "payout_method": request.payout_method.value,
                "requires_payout": requires_payout,
            },
        )

        if not requires_payout:
            # Nothing to transfer: the deposit was fully consumed by the deductions.
            # The SRS still expects a NOC "upon payment", so we settle it immediately.
            settlement.status = SettlementStatus.COMPLETED
            settlement.payment_completed_at = now
            settlement.payment_provider = "internal"
            settlement.payment_reference = f"offset-{settlement.id}"
            await self.confirm_settlement(
                workflow, settlement, ctx=ctx.as_system(), provider_reference=None
            )
            return settlement

        settlement.status = SettlementStatus.PROCESSING
        await self._session.flush()

        if self._dispatcher is not None:
            dispatcher = self._dispatcher
            workflow_id, request_id = workflow.id, ctx.request_id

            async def _dispatch() -> None:
                await dispatcher.dispatch(workflow_id, request_id)

            self._uow.after_commit("payout:initiate", _dispatch)

        return settlement

    # ----------------------------------------------- provider outcome
    async def confirm_settlement(
        self,
        workflow: ExitWorkflow,
        settlement: Settlement,
        *,
        ctx: RequestContext,
        provider_reference: str | None,
    ) -> Settlement:
        """Record a successful payout and hand off to NOC issuance."""
        if settlement.status is SettlementStatus.COMPLETED and (
            workflow.state
            in (
                ExitWorkflowState.SETTLEMENT_COMPLETED,
                ExitWorkflowState.NOC_ISSUED,
                ExitWorkflowState.COMPLETED,
            )
        ):
            return settlement  # already confirmed; webhooks are at-least-once

        now = self._clock.now()
        settlement.status = SettlementStatus.COMPLETED
        settlement.payment_completed_at = settlement.payment_completed_at or now
        if provider_reference:
            settlement.payment_reference = provider_reference
        settlement.failure_code = None
        settlement.failure_reason = None

        self._engine.transition(
            workflow,
            action="settlement_succeeded",
            ctx=ctx,
            event_type=ev.SETTLEMENT_PAYMENT_SUCCEEDED,
            event_payload={
                "settlement_id": str(settlement.id),
                "net_refund_amount": str(settlement.net_refund_amount),
                "payment_reference": settlement.payment_reference,
            },
        )
        self._notifications.enqueue(
            template=NotificationTemplate.TENANT_REFUND_PAID,
            recipients=(tenant_recipient(workflow),),
            context={
                **base_context(workflow),
                "net_refund_amount": str(settlement.net_refund_amount),
                "currency": settlement.currency,
                "payment_reference": settlement.payment_reference,
            },
            dedupe_key=f"{settlement.id}:paid",
        )

        # SRS O16: "digital Exit NOC auto-generated upon payment". Issued in the same
        # transaction as the confirmation, so a client can never observe a paid
        # settlement without its certificate.
        await self._noc_issuer.issue(workflow, ctx=ctx.as_system())
        return settlement

    async def fail_settlement(
        self,
        workflow: ExitWorkflow,
        settlement: Settlement,
        *,
        ctx: RequestContext,
        failure_code: str | None,
        failure_reason: str | None,
    ) -> Settlement:
        """Record a failed payout and return the settlement to the owner."""
        if settlement.status is not SettlementStatus.PROCESSING:
            return settlement

        settlement.status = SettlementStatus.PENDING_APPROVAL
        settlement.failure_code = failure_code
        settlement.failure_reason = failure_reason
        settlement.payment_completed_at = None

        self._engine.transition(
            workflow,
            action="settlement_failed",
            ctx=ctx,
            note=failure_reason,
            event_type=ev.SETTLEMENT_PAYMENT_FAILED,
            event_payload={
                "settlement_id": str(settlement.id),
                "failure_code": failure_code,
                "failure_reason": failure_reason,
                "attempts": settlement.payment_attempts,
            },
        )
        self._notifications.enqueue(
            template=NotificationTemplate.OWNER_REFUND_FAILED,
            recipients=(owner_recipient(workflow),),
            context={
                **base_context(workflow),
                "failure_code": failure_code,
                "failure_reason": failure_reason,
            },
            dedupe_key=f"{settlement.id}:failed:{settlement.payment_attempts}",
        )
        return settlement

    # ---------------------------------------------------------- helpers
    def _lines_from_inspection(
        self, inspection: Inspection | None
    ) -> list[DeductionLineResponse]:
        """Chargeable damage items for the current inspection round."""
        if inspection is None:
            return []
        lines: list[DeductionLineResponse] = []
        for item in inspection.damage_items:
            if item.round_number != inspection.round_number:
                continue
            amount = quantize(item.chargeable_amount)
            if amount <= ZERO:
                continue
            lines.append(
                DeductionLineResponse(
                    damage_item_id=item.id,
                    category=item.category,
                    description=item.description,
                    amount=amount,
                )
            )
        return lines

    @staticmethod
    def to_response_lines(settlement: Settlement) -> list[DeductionLineResponse]:
        return [
            DeductionLineResponse(
                id=line.id,
                damage_item_id=line.damage_item_id,
                category=DeductionCategory(line.category),
                description=line.description,
                amount=line.amount,
            )
            for line in settlement.deductions
        ]

    @staticmethod
    def assert_amount_matches(expected: Decimal, actual: Decimal) -> None:
        if quantize(expected) != quantize(actual):
            raise BusinessRuleViolationError(
                rule="SETTLEMENT_AMOUNT_MISMATCH",
                message="The settlement amount does not match the expected figure.",
                details={"expected": str(expected), "actual": str(actual)},
            )
