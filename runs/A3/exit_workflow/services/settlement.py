"""O16 — deduction finalisation, deposit payout and settlement reconciliation.

    "Inspection agency uploads damage report with photos; system calculates
     deduction from security deposit; owner clicks 'Pay Deposit' (deposit minus
     damage); digital Exit NOC auto-generated upon payment."
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.errors import ConflictError, NotFoundError, UpstreamServiceError, ValidationError
from exit_workflow.domain import policy
from exit_workflow.domain.enums import (
    DamageReportStatus,
    ExitWorkflowStatus,
    PaymentStatus,
    PayoutMethod,
    SettlementStatus,
)
from exit_workflow.integrations.payments import PaymentGateway, PayoutRequest
from exit_workflow.models.inspection import DamageReport
from exit_workflow.models.settlement import PaymentTransaction, Settlement
from exit_workflow.models.workflow import ExitWorkflow
from exit_workflow.services import access
from exit_workflow.services.audit import AuditRecorder
from exit_workflow.services.context import ServiceContext
from exit_workflow.services.events import AggregateType, EventRecorder, EventType
from exit_workflow.services.inspection import InspectionService
from exit_workflow.services.noc import NocService
from exit_workflow.services.notifications import NotificationService, Template
from exit_workflow.services.transitions import apply_transition


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    """Result of a payout attempt.

    Failures are returned rather than raised so the failed attempt is committed
    to the ledger; the router turns a non-successful outcome into a 502.
    """

    settlement: Settlement
    transaction: PaymentTransaction
    succeeded: bool
    indeterminate: bool = False
    failure_code: str | None = None
    failure_message: str | None = None


class SettlementService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        ctx: ServiceContext,
        *,
        audit: AuditRecorder,
        events: EventRecorder,
        notifications: NotificationService,
        gateway: PaymentGateway,
        noc: NocService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._ctx = ctx
        self._audit = audit
        self._events = events
        self._notifications = notifications
        self._gateway = gateway
        self._noc = noc

    # -- read --------------------------------------------------------------
    async def get(self, workflow: ExitWorkflow, *, for_update: bool = False) -> Settlement:
        stmt = select(Settlement).where(Settlement.workflow_id == workflow.id)
        if for_update:
            stmt = stmt.with_for_update(of=Settlement)
        settlement = (await self._session.execute(stmt)).scalars().first()
        if settlement is None:
            raise NotFoundError("No settlement has been calculated for this exit workflow yet.")
        return settlement

    async def preview(self, workflow: ExitWorkflow, report: DamageReport | None) -> policy.SettlementBreakdown:
        """What the settlement *would* be, for display during damage review."""

        deduction = Decimal("0.00")
        if report is not None:
            deduction = report.finalized_total if report.finalized_total is not None else report.assessed_total
        return policy.compute_settlement(
            security_deposit_amount=workflow.security_deposit_amount,
            total_deduction_amount=deduction,
            settings=self._settings,
            currency=workflow.currency,
        )

    # -- owner finalises the deduction (DAMAGE_REVIEW -> SETTLEMENT_PENDING)
    async def finalize_deduction(
        self,
        workflow: ExitWorkflow,
        report: DamageReport,
        *,
        deduction_amount: Decimal | None = None,
        adjustment_reason: str | None = None,
        payout_method: PayoutMethod = PayoutMethod.BANK_TRANSFER,
        payout_destination_token: str | None = None,
        payout_destination_masked: str | None = None,
    ) -> Settlement:
        access.ensure_is_owner(workflow, self._ctx.require_principal())
        InspectionService.ensure_ready_for_settlement(report)

        assessed = report.assessed_total
        final = assessed if deduction_amount is None else policy.validate_owner_adjustment(
            assessed, deduction_amount
        )
        if final != assessed and not (adjustment_reason or "").strip():
            raise ValidationError(
                "An adjustment reason is required when the deduction differs from the "
                "assessed amount.",
                extra={"field": "adjustment_reason", "assessed_amount": str(assessed)},
            )

        breakdown = policy.compute_settlement(
            security_deposit_amount=workflow.security_deposit_amount,
            total_deduction_amount=final,
            settings=self._settings,
            currency=workflow.currency,
        )

        now = utcnow()
        report.status = DamageReportStatus.FINALIZED
        report.finalized_total = final
        report.finalized_at = now
        report.finalized_by = self._ctx.actor_id
        report.adjustment_reason = adjustment_reason

        settlement = (
            await self._session.execute(
                select(Settlement).where(Settlement.workflow_id == workflow.id)
            )
        ).scalars().first()
        if settlement is None:
            settlement = Settlement(workflow_id=workflow.id, computed_at=now)
            self._session.add(settlement)
        elif settlement.status is SettlementStatus.PAID:  # pragma: no cover - guarded upstream
            raise ConflictError("This settlement has already been paid.")

        settlement.damage_report_id = report.id
        settlement.status = SettlementStatus.PENDING
        settlement.currency = breakdown.currency
        settlement.security_deposit_amount = breakdown.security_deposit_amount
        settlement.total_deduction_amount = breakdown.total_deduction_amount
        settlement.refund_amount = breakdown.refund_amount
        settlement.balance_due_from_tenant = breakdown.balance_due_from_tenant
        settlement.computed_at = now
        settlement.finalized_at = now
        settlement.finalized_by = self._ctx.actor_id
        settlement.adjustment_reason = adjustment_reason
        settlement.payout_method = (
            PayoutMethod.NONE if not breakdown.requires_payout else payout_method
        )
        settlement.payout_destination_token = payout_destination_token
        settlement.payout_destination_masked = payout_destination_masked

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.SETTLEMENT_PENDING,
            reason="Owner finalised the damage deduction",
            attributes={
                "total_deduction_amount": str(breakdown.total_deduction_amount),
                "refund_amount": str(breakdown.refund_amount),
            },
        )
        workflow.damage_review_completed_at = now

        self._audit.record(
            self._ctx,
            action="settlement.computed",
            entity_type="settlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            changes={
                "assessed_total": assessed,
                "total_deduction_amount": breakdown.total_deduction_amount,
                "refund_amount": breakdown.refund_amount,
                "balance_due_from_tenant": breakdown.balance_due_from_tenant,
                "adjustment_reason": adjustment_reason,
            },
        )
        for event_type, aggregate in (
            (EventType.DAMAGE_REPORT_FINALIZED, (AggregateType.DAMAGE_REPORT, report.id)),
            (EventType.SETTLEMENT_COMPUTED, (AggregateType.SETTLEMENT, settlement.id)),
        ):
            self._events.emit(
                self._ctx,
                event_type=event_type,
                aggregate_type=aggregate[0],
                aggregate_id=aggregate[1],
                workflow_id=workflow.id,
                payload={
                    "settlement_id": settlement.id,
                    "damage_report_id": report.id,
                    "currency": settlement.currency,
                    "security_deposit_amount": settlement.security_deposit_amount,
                    "total_deduction_amount": settlement.total_deduction_amount,
                    "refund_amount": settlement.refund_amount,
                    "balance_due_from_tenant": settlement.balance_due_from_tenant,
                },
            )
        self._notifications.enqueue(
            template=Template.OWNER_SETTLEMENT_READY,
            recipient=workflow.owner_email,
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "owner_name": workflow.owner_name,
                "property_address": workflow.property_address,
                "currency": settlement.currency,
                "security_deposit_amount": settlement.security_deposit_amount,
                "total_deduction_amount": settlement.total_deduction_amount,
                "refund_amount": settlement.refund_amount,
            },
        )
        await self._session.flush()
        return settlement

    # -- "Pay Deposit" -----------------------------------------------------
    async def pay(
        self,
        workflow: ExitWorkflow,
        settlement: Settlement,
        *,
        idempotency_key: str,
        payout_destination_token: str | None = None,
        payout_destination_masked: str | None = None,
    ) -> PaymentOutcome:
        access.ensure_is_owner(workflow, self._ctx.require_principal())

        if workflow.status is not ExitWorkflowStatus.SETTLEMENT_PENDING:
            raise ConflictError(
                f"Exit workflow is {workflow.status.value}; the deposit can only be paid from "
                "SETTLEMENT_PENDING."
            )
        if settlement.status is SettlementStatus.PAID:
            raise ConflictError("This deposit settlement has already been paid.")
        if settlement.status is SettlementStatus.PROCESSING:
            raise ConflictError(
                "A payout for this settlement is already in flight and awaiting "
                "reconciliation.",
                extra={"settlement_id": str(settlement.id)},
            )

        if payout_destination_token:
            settlement.payout_destination_token = payout_destination_token
            settlement.payout_destination_masked = payout_destination_masked

        now = utcnow()
        settlement.attempt_count += 1
        transaction = PaymentTransaction(
            # Assigned via the relationship so the loaded collection (and any
            # response built from it) includes this attempt immediately.
            settlement=settlement,
            idempotency_key=idempotency_key,
            status=PaymentStatus.PENDING,
            amount=settlement.refund_amount,
            currency=settlement.currency,
            gateway=getattr(self._gateway, "name", "unknown"),
            initiated_by=self._ctx.require_principal().subject_id,
            initiated_at=now,
        )
        self._session.add(transaction)
        await self._session.flush()

        if settlement.refund_amount <= 0:
            # Damage consumed the whole deposit: there is nothing to transfer,
            # but the settlement is still completed so the NOC can issue.
            transaction.status = PaymentStatus.SUCCEEDED
            transaction.completed_at = now
            transaction.gateway = "none"
            transaction.gateway_response = {"reason": "zero_refund"}
            await self._complete(workflow, settlement, transaction)
            return PaymentOutcome(settlement, transaction, succeeded=True)

        request = PayoutRequest(
            transaction_id=transaction.id,
            amount=settlement.refund_amount,
            currency=settlement.currency,
            destination_token=settlement.payout_destination_token,
            reference=workflow.reference,
            description=f"Security deposit refund for exit {workflow.reference}",
            metadata={
                "workflow_id": str(workflow.id),
                "settlement_id": str(settlement.id),
                "tenant_id": str(workflow.tenant_id),
            },
        )

        try:
            result = await self._gateway.payout(request)
        except UpstreamServiceError as exc:
            # Indeterminate: the transfer may have happened. Never retried
            # automatically; a reconciliation decides the outcome.
            settlement.status = SettlementStatus.PROCESSING
            settlement.failure_reason = exc.detail
            transaction.failure_message = exc.detail
            transaction.gateway_response = {"error": exc.detail}
            self._audit.record(
                self._ctx,
                action="settlement.payment_indeterminate",
                entity_type="settlement",
                entity_id=settlement.id,
                workflow_id=workflow.id,
                changes={"transaction_id": transaction.id, "error": exc.detail},
            )
            return PaymentOutcome(
                settlement,
                transaction,
                succeeded=False,
                indeterminate=True,
                failure_code="GATEWAY_UNAVAILABLE",
                failure_message=exc.detail,
            )

        if not result.succeeded:
            transaction.status = PaymentStatus.FAILED
            transaction.completed_at = utcnow()
            transaction.failure_code = result.failure_code
            transaction.failure_message = result.failure_message
            transaction.gateway_response = result.raw
            settlement.status = SettlementStatus.PENDING
            settlement.failure_reason = result.failure_message

            self._audit.record(
                self._ctx,
                action="settlement.payment_failed",
                entity_type="settlement",
                entity_id=settlement.id,
                workflow_id=workflow.id,
                changes={
                    "transaction_id": transaction.id,
                    "failure_code": result.failure_code,
                    "failure_message": result.failure_message,
                },
            )
            self._events.emit(
                self._ctx,
                event_type=EventType.SETTLEMENT_PAYMENT_FAILED,
                aggregate_type=AggregateType.SETTLEMENT,
                aggregate_id=settlement.id,
                workflow_id=workflow.id,
                payload={
                    "settlement_id": settlement.id,
                    "transaction_id": transaction.id,
                    "failure_code": result.failure_code,
                },
            )
            return PaymentOutcome(
                settlement,
                transaction,
                succeeded=False,
                failure_code=result.failure_code,
                failure_message=result.failure_message,
            )

        transaction.status = PaymentStatus.SUCCEEDED
        transaction.completed_at = utcnow()
        transaction.gateway_reference = result.gateway_reference
        transaction.gateway_response = result.raw
        await self._complete(workflow, settlement, transaction)
        return PaymentOutcome(settlement, transaction, succeeded=True)

    async def _complete(
        self, workflow: ExitWorkflow, settlement: Settlement, transaction: PaymentTransaction
    ) -> None:
        """Payment succeeded: settle, issue the NOC, complete the workflow."""

        now = utcnow()
        settlement.status = SettlementStatus.PAID
        settlement.paid_at = now
        settlement.paid_by = self._ctx.actor_id
        settlement.payment_reference = transaction.gateway_reference or str(transaction.id)
        settlement.failure_reason = None

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.SETTLEMENT_COMPLETED,
            reason="Deposit settlement paid",
            attributes={"payment_reference": settlement.payment_reference},
        )
        workflow.settlement_completed_at = now

        self._audit.record(
            self._ctx,
            action="settlement.paid",
            entity_type="settlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            changes={
                "transaction_id": transaction.id,
                "amount": transaction.amount,
                "payment_reference": settlement.payment_reference,
            },
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.SETTLEMENT_PAID,
            aggregate_type=AggregateType.SETTLEMENT,
            aggregate_id=settlement.id,
            workflow_id=workflow.id,
            payload={
                "settlement_id": settlement.id,
                "transaction_id": transaction.id,
                "amount": transaction.amount,
                "currency": settlement.currency,
                "payment_reference": settlement.payment_reference,
                "tenant_id": workflow.tenant_id,
            },
        )
        self._notifications.enqueue(
            template=Template.TENANT_SETTLEMENT_PAID,
            recipient=workflow.tenant_email,
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "tenant_name": workflow.tenant_name,
                "property_address": workflow.property_address,
                "currency": settlement.currency,
                "refund_amount": settlement.refund_amount,
                "payment_reference": settlement.payment_reference,
            },
        )

        # O16: the NOC is generated automatically upon payment, which also
        # completes the workflow and releases the BR-1 lock.
        await self._noc.issue(workflow, settlement)

    # -- reconciliation of indeterminate payouts ---------------------------
    async def reconcile(
        self,
        workflow: ExitWorkflow,
        settlement: Settlement,
        *,
        transaction_id: uuid.UUID,
        succeeded: bool,
        gateway_reference: str | None = None,
        note: str | None = None,
    ) -> Settlement:
        """Administrator resolves a payout whose outcome was unknown."""

        principal = self._ctx.require_principal()
        if not principal.is_admin:
            raise ConflictError("Only an administrator may reconcile a settlement.")
        if settlement.status is not SettlementStatus.PROCESSING:
            raise ConflictError(
                f"Settlement is {settlement.status.value}; only PROCESSING settlements need "
                "reconciliation."
            )

        transaction = next(
            (t for t in settlement.transactions if t.id == transaction_id), None
        )
        if transaction is None:
            raise NotFoundError("Payment transaction not found for this settlement.")

        transaction.completed_at = utcnow()
        transaction.gateway_response = dict(transaction.gateway_response or {}) | {
            "reconciled": True,
            "note": note,
        }
        if succeeded:
            transaction.status = PaymentStatus.SUCCEEDED
            transaction.gateway_reference = gateway_reference or transaction.gateway_reference
            await self._complete(workflow, settlement, transaction)
        else:
            transaction.status = PaymentStatus.FAILED
            transaction.failure_code = "RECONCILED_FAILED"
            transaction.failure_message = note
            settlement.status = SettlementStatus.PENDING
            settlement.failure_reason = note

        self._audit.record(
            self._ctx,
            action="settlement.reconciled",
            entity_type="settlement",
            entity_id=settlement.id,
            workflow_id=workflow.id,
            changes={"transaction_id": transaction_id, "succeeded": succeeded, "note": note},
        )
        await self._session.flush()
        return settlement
