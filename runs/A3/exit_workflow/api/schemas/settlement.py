"""Deposit settlement schemas (O16)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from exit_workflow.api.schemas.common import ApiModel, Money, MoneyIn
from exit_workflow.domain.enums import PaymentStatus, PayoutMethod, SettlementStatus


class FinalizeDeductionRequest(BaseModel):
    """Owner confirms the deduction. Omit ``deduction_amount`` to accept the
    agency's assessment in full; it may be reduced but never increased."""

    deduction_amount: MoneyIn | None = None
    adjustment_reason: str | None = Field(default=None, max_length=2000)
    payout_method: PayoutMethod = PayoutMethod.BANK_TRANSFER
    payout_destination_token: str | None = Field(default=None, max_length=128)
    payout_destination_masked: str | None = Field(default=None, max_length=64)

    model_config = {"extra": "forbid"}


class PayDepositRequest(BaseModel):
    payout_destination_token: str | None = Field(default=None, max_length=128)
    payout_destination_masked: str | None = Field(default=None, max_length=64)

    model_config = {"extra": "forbid"}


class ReconcileSettlementRequest(BaseModel):
    transaction_id: uuid.UUID
    succeeded: bool
    gateway_reference: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}


class PaymentTransactionResponse(ApiModel):
    id: uuid.UUID
    status: PaymentStatus
    amount: Money
    currency: str
    gateway: str
    gateway_reference: str | None
    failure_code: str | None
    failure_message: str | None
    initiated_at: datetime
    completed_at: datetime | None


class SettlementResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    damage_report_id: uuid.UUID | None
    status: SettlementStatus
    currency: str
    security_deposit_amount: Money
    total_deduction_amount: Money
    refund_amount: Money
    balance_due_from_tenant: Money
    payout_method: PayoutMethod
    payout_destination_masked: str | None
    computed_at: datetime
    finalized_at: datetime | None
    adjustment_reason: str | None
    paid_at: datetime | None
    payment_reference: str | None
    failure_reason: str | None
    attempt_count: int
    transactions: list[PaymentTransactionResponse] = []


class SettlementPreviewResponse(BaseModel):
    """What "Pay Deposit" would settle, given the current damage assessment."""

    currency: str
    security_deposit_amount: Money
    total_deduction_amount: Money
    refund_amount: Money
    balance_due_from_tenant: Money
    is_final: bool
    damage_report_id: uuid.UUID | None = None
