"""Deposit settlement and refund processing (SRS O16).

Sequence: the agency's damage report produces a DRAFT settlement -> the owner reviews and
approves it, making it PAYABLE -> each non-zero leg is paid -> the settlement CLOSES -> the
Exit NOC is issued automatically, exactly as the BRD describes ("digital Exit NOC
auto-generated upon payment").
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from app.domain.settlement import DeductionInput, compute_settlement
from app.domain.states import ExitWorkflowState
from app.errors import (
    AuthorizationError,
    ConflictError,
    IdempotencyConflict,
    NotFoundError,
    SettlementNotPayable,
    ValidationError,
)
from app.models.audit import AuditAction
from app.models.base import utcnow
from app.models.inspection import DamageReport
from app.models.settlement import (
    DepositSettlement,
    PaymentLeg,
    PaymentStatus,
    PaymentTransaction,
    SettlementStatus,
)
from app.models.workflow import ExitWorkflow
from app.money import format_aed
from app.ports.events import EventType
from app.ports.notifications import Channel, Notification, NotificationTemplate
from app.ports.payments import InternalLedgerGateway, PaymentGateway, PaymentRequest
from app.security import PrincipalRole
from app.services.base import ServiceBase
from app.services.workflow_service import WorkflowService

S = ExitWorkflowState

#: Which role is expected to fund each leg.
_LEG_PAYER: dict[PaymentLeg, PrincipalRole] = {
    PaymentLeg.OWNER_REFUND: PrincipalRole.OWNER,
    PaymentLeg.TENANT_BALANCE: PrincipalRole.TENANT,
}


class SettlementService(ServiceBase):
    def __init__(self, *args, gateway: PaymentGateway | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.gateway = gateway or InternalLedgerGateway()

    def _workflow_service(self) -> WorkflowService:
        return WorkflowService(
            self.session, self.ctx, self.settings, recorder=self.events, audit=self.audit
        )

    # --- computation --------------------------------------------------------------------

    async def compute_for_report(
        self, workflow: ExitWorkflow, report: DamageReport
    ) -> DepositSettlement:
        """Derive the settlement figures from a damage report (O16).

        Reuses the workflow's existing settlement row if one is present — a re-inspection
        voids the previous settlement rather than creating a second one.
        """
        await self.session.refresh(report, ["line_items"])
        deductions = [
            DeductionInput(code=item.code, description=item.description, amount_fils=item.amount_fils)
            for item in report.line_items
        ]
        breakdown = compute_settlement(
            deposit_fils=workflow.deposit_snapshot_fils, deductions=deductions
        )

        settlement = workflow.settlement
        if settlement is None:
            settlement = DepositSettlement(
                workflow_id=workflow.id, deposit_fils=breakdown.deposit_fils, payments=[]
            )
            self.session.add(settlement)
            workflow.settlement = settlement
        elif settlement.status not in (SettlementStatus.DRAFT, SettlementStatus.VOID):
            raise ConflictError(
                "settlement for this workflow is no longer editable",
                details={"settlement_status": settlement.status.value},
            )

        settlement.damage_report_id = report.id
        settlement.status = SettlementStatus.DRAFT
        settlement.currency = self.settings.currency
        settlement.deposit_fils = breakdown.deposit_fils
        settlement.total_deductions_fils = breakdown.total_deductions_fils
        settlement.refund_fils = breakdown.refund_fils
        settlement.balance_due_fils = breakdown.balance_due_fils
        settlement.refund_settled_at = None
        settlement.balance_settled_at = None
        settlement.closed_at = None
        settlement.approved_at = None
        settlement.void_reason = None
        settlement.computed_at = utcnow()
        settlement.breakdown = {
            **breakdown.as_dict(),
            "display": breakdown.as_display(),
            "report_id": str(report.id),
            "line_items": [
                {
                    "code": item.code,
                    "description": item.description,
                    "severity": item.severity.value,
                    "amount_fils": item.amount_fils,
                    "location": item.location,
                }
                for item in report.line_items
            ],
        }
        await self.session.flush()

        self.audit.record(
            AuditAction.SETTLEMENT_COMPUTED,
            entity_type="DepositSettlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            payload=breakdown.as_dict(),
        )
        self._workflow_service().emit(
            workflow,
            EventType.SETTLEMENT_COMPUTED,
            {"settlement_id": str(settlement.id), **breakdown.as_dict()},
        )
        return settlement

    async def void_for_reinspection(self, workflow: ExitWorkflow, *, reason: str) -> None:
        settlement = workflow.settlement
        if settlement is None:
            return
        if settlement.status is SettlementStatus.CLOSED:
            raise ConflictError("a closed settlement cannot be voided")
        settlement.status = SettlementStatus.VOID
        settlement.void_reason = reason
        self.audit.record(
            AuditAction.SETTLEMENT_DISPUTED,
            entity_type="DepositSettlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            payload={"reason": reason},
        )

    # --- approval -------------------------------------------------------------------------

    async def approve(self, workflow_id: uuid.UUID) -> DepositSettlement:
        """Owner accepts the deductions; the settlement becomes payable (T13 step 9)."""
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_tenant=False)

        settlement = self._require_settlement(workflow)
        if settlement.status is not SettlementStatus.DRAFT:
            raise ConflictError(
                "only a draft settlement can be approved",
                details={"settlement_status": settlement.status.value},
            )

        workflows = self._workflow_service()
        workflows.transition(workflow, S.PENDING_SETTLEMENT, note="settlement approved by owner")

        settlement.status = SettlementStatus.PAYABLE
        settlement.approved_at = utcnow()
        settlement.approved_by_type = self.ctx.actor_type
        settlement.approved_by_id = self.ctx.actor_id
        # A zero-valued leg has nothing to pay, so it is satisfied on approval.
        now = utcnow()
        if settlement.refund_fils == 0:
            settlement.refund_settled_at = now
        if settlement.balance_due_fils == 0:
            settlement.balance_settled_at = now

        self.audit.record(
            AuditAction.SETTLEMENT_APPROVED,
            entity_type="DepositSettlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            payload={
                "refund_fils": settlement.refund_fils,
                "balance_due_fils": settlement.balance_due_fils,
            },
        )
        workflows.emit(
            workflow,
            EventType.SETTLEMENT_APPROVED,
            {
                "settlement_id": str(settlement.id),
                "refund_fils": settlement.refund_fils,
                "balance_due_fils": settlement.balance_due_fils,
            },
        )

        if settlement.refund_fils > 0:
            self.notify(
                workflow,
                Notification(
                    template=NotificationTemplate.OWNER_SETTLEMENT_PAYABLE,
                    channel=Channel.EMAIL,
                    recipient=workflow.owner.email,
                    subject=f"Deposit refund due for {workflow.reference}",
                    context={
                        "reference": workflow.reference,
                        "refund": format_aed(settlement.refund_fils),
                    },
                ),
            )
        if settlement.balance_due_fils > 0:
            self.notify(
                workflow,
                Notification(
                    template=NotificationTemplate.TENANT_BALANCE_DUE,
                    channel=Channel.EMAIL,
                    recipient=workflow.tenant.email,
                    subject=f"Balance due for {workflow.reference}",
                    context={
                        "reference": workflow.reference,
                        "balance_due": format_aed(settlement.balance_due_fils),
                    },
                ),
            )

        await self._close_if_fully_settled(workflow, settlement)
        return settlement

    async def dispute(self, workflow_id: uuid.UUID, *, reason: str) -> ExitWorkflow:
        """Tenant contests an approved settlement, sending it back to damage review."""
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_owner=False)
        if not reason.strip():
            raise ValidationError("a dispute reason is required", details={"field": "reason"})

        settlement = self._require_settlement(workflow)
        if settlement.payments and any(p.status is PaymentStatus.SUCCEEDED for p in settlement.payments):
            raise ConflictError("a settlement with completed payments cannot be disputed")

        workflows = self._workflow_service()
        workflows.transition(workflow, S.DAMAGE_REVIEW, note=f"settlement disputed: {reason}")
        settlement.status = SettlementStatus.DRAFT
        settlement.approved_at = None
        settlement.refund_settled_at = None
        settlement.balance_settled_at = None

        self.audit.record(
            AuditAction.SETTLEMENT_DISPUTED,
            entity_type="DepositSettlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            payload={"reason": reason},
        )
        return workflow

    # --- payment ---------------------------------------------------------------------------

    async def pay(
        self,
        workflow_id: uuid.UUID,
        *,
        leg: PaymentLeg,
        idempotency_key: str,
    ) -> PaymentTransaction:
        """Execute one settlement leg. Safe to retry with the same ``idempotency_key``."""
        if not idempotency_key.strip():
            raise ValidationError("an idempotency key is required to take a payment")

        workflow = await self.load_workflow(workflow_id, for_update=True)
        settlement = self._require_settlement(workflow)
        self._authorize_payer(workflow, leg)

        # Replay protection first: a retried click must never create a second movement.
        existing = await self.session.scalar(
            sa.select(PaymentTransaction).where(
                PaymentTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if existing.settlement_id != settlement.id or existing.leg is not leg:
                raise IdempotencyConflict(
                    "this idempotency key was already used for a different payment",
                    details={"idempotency_key": idempotency_key},
                )
            return existing

        if settlement.status is not SettlementStatus.PAYABLE:
            raise SettlementNotPayable(
                "settlement is not payable in its current status",
                details={"settlement_status": settlement.status.value},
            )
        if workflow.state is not S.PENDING_SETTLEMENT:
            raise ConflictError(
                "payment is only accepted while the workflow is pending settlement",
                details={"state": workflow.state.value},
            )

        amount = settlement.leg_amount(leg)
        if amount <= 0:
            raise SettlementNotPayable(
                "this settlement leg has nothing to pay", details={"leg": leg.value}
            )
        already_settled = (
            settlement.refund_settled_at
            if leg is PaymentLeg.OWNER_REFUND
            else settlement.balance_settled_at
        )
        if already_settled is not None:
            raise ConflictError("this settlement leg is already paid", details={"leg": leg.value})

        payer_id, payee_id = (
            (workflow.owner_id, workflow.tenant_id)
            if leg is PaymentLeg.OWNER_REFUND
            else (workflow.tenant_id, workflow.owner_id)
        )
        transaction = PaymentTransaction(
            settlement_id=settlement.id,
            workflow_id=workflow.id,
            leg=leg,
            status=PaymentStatus.PENDING,
            amount_fils=amount,
            currency=settlement.currency,
            idempotency_key=idempotency_key,
            initiated_by_type=self.ctx.actor_type,
            initiated_by_id=self.ctx.actor_id,
        )
        self.session.add(transaction)
        settlement.payments.append(transaction)
        await self.session.flush()

        self.audit.record(
            AuditAction.PAYMENT_INITIATED,
            entity_type="PaymentTransaction",
            entity_id=transaction.id,
            workflow_id=workflow.id,
            payload={"leg": leg.value, "amount_fils": amount},
        )

        result = await self.gateway.execute(
            PaymentRequest(
                amount_fils=amount,
                currency=settlement.currency,
                idempotency_key=idempotency_key,
                reference=workflow.reference,
                payer_id=payer_id,
                payee_id=payee_id,
                description=f"Exit settlement {leg.value} for {workflow.reference}",
            )
        )
        transaction.provider = result.provider
        transaction.provider_reference = result.provider_reference

        if not result.succeeded:
            transaction.status = PaymentStatus.FAILED
            transaction.failure_reason = result.failure_reason or "payment declined"
            transaction.completed_at = utcnow()
            self.audit.record(
                AuditAction.PAYMENT_FAILED,
                entity_type="PaymentTransaction",
                entity_id=transaction.id,
                workflow_id=workflow.id,
                payload={"leg": leg.value, "reason": transaction.failure_reason},
            )
            raise ConflictError(
                "payment was not accepted by the provider",
                details={"leg": leg.value, "reason": transaction.failure_reason},
            )

        transaction.status = PaymentStatus.SUCCEEDED
        transaction.completed_at = utcnow()
        if leg is PaymentLeg.OWNER_REFUND:
            settlement.refund_settled_at = utcnow()
        else:
            settlement.balance_settled_at = utcnow()

        self.audit.record(
            AuditAction.PAYMENT_SUCCEEDED,
            entity_type="PaymentTransaction",
            entity_id=transaction.id,
            workflow_id=workflow.id,
            payload={
                "leg": leg.value,
                "amount_fils": amount,
                "provider_reference": result.provider_reference,
            },
        )
        self._workflow_service().emit(
            workflow,
            EventType.PAYMENT_SUCCEEDED,
            {
                "payment_id": str(transaction.id),
                "leg": leg.value,
                "amount_fils": amount,
            },
        )
        if leg is PaymentLeg.OWNER_REFUND:
            self.notify(
                workflow,
                Notification(
                    template=NotificationTemplate.TENANT_REFUND_PAID,
                    channel=Channel.EMAIL,
                    recipient=workflow.tenant.email,
                    subject=f"Deposit refund released for {workflow.reference}",
                    context={"reference": workflow.reference, "amount": format_aed(amount)},
                ),
            )

        await self._close_if_fully_settled(workflow, settlement)
        return transaction

    # --- closing ------------------------------------------------------------------------------

    async def _close_if_fully_settled(
        self, workflow: ExitWorkflow, settlement: DepositSettlement
    ) -> None:
        """Close the settlement and issue the NOC once every leg is satisfied."""
        if not settlement.is_fully_settled or settlement.status is SettlementStatus.CLOSED:
            return

        settlement.status = SettlementStatus.CLOSED
        settlement.closed_at = utcnow()

        workflows = self._workflow_service()
        workflows.transition(workflow, S.SETTLED, note="settlement closed")
        workflow.settled_at = utcnow()

        self.audit.record(
            AuditAction.SETTLEMENT_CLOSED,
            entity_type="DepositSettlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            payload={
                "refund_fils": settlement.refund_fils,
                "balance_due_fils": settlement.balance_due_fils,
            },
        )
        workflows.emit(
            workflow, EventType.SETTLEMENT_CLOSED, {"settlement_id": str(settlement.id)}
        )

        # O16: "digital Exit NOC auto-generated upon payment".
        from app.services.noc_service import NOCService

        noc_service = NOCService(
            self.session, self.ctx, self.settings, recorder=self.events, audit=self.audit
        )
        await noc_service.issue(workflow, settlement)

    # --- helpers -------------------------------------------------------------------------------

    def _require_settlement(self, workflow: ExitWorkflow) -> DepositSettlement:
        if workflow.settlement is None:
            raise NotFoundError(
                "no settlement has been computed for this workflow",
                details={"workflow_id": str(workflow.id)},
            )
        return workflow.settlement

    def _authorize_payer(self, workflow: ExitWorkflow, leg: PaymentLeg) -> None:
        principal = self.ctx.require_principal()
        if principal.is_admin:
            return
        expected_role = _LEG_PAYER[leg]
        expected_id = (
            workflow.owner_id if expected_role is PrincipalRole.OWNER else workflow.tenant_id
        )
        if principal.role is not expected_role or principal.id != expected_id:
            raise AuthorizationError(
                "this settlement leg must be paid by the counterparty who owes it",
                details={"leg": leg.value, "required_role": expected_role.value},
            )

    async def get(self, workflow_id: uuid.UUID) -> DepositSettlement:
        workflow = await self.load_workflow(workflow_id)
        self.authorize_participant(workflow, allow_agency=True)
        return self._require_settlement(workflow)
