"""Deposit settlement schemas (SRS O16, T13 step 9)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.domain.enums import DeductionCategory, PayoutMethod, SettlementStatus
from app.schemas.common import ApiModel, CommandModel, LongText, Money, ShortText


class ManualDeductionRequest(CommandModel):
    """A charge the inspection cannot see -- unpaid rent, utilities, admin fees."""

    category: DeductionCategory
    description: LongText
    amount: Money


class FinaliseSettlementRequest(CommandModel):
    """Closes damage review and freezes the arithmetic for payment."""

    manual_deductions: list[ManualDeductionRequest] = Field(
        default_factory=list, max_length=50
    )
    owner_note: LongText | None = None
    #: Owner confirmation that the figures shown match what they intend to pay. Guards
    #: against a stale client submitting against numbers that have since changed.
    expected_net_refund: Money | None = Field(
        default=None,
        description=(
            "If supplied, the request is rejected unless the computed refund matches. "
            "Send the figure the owner was shown."
        ),
    )


class PayDepositRequest(CommandModel):
    """SRS O16: "owner clicks 'Pay Deposit' (deposit minus damage)"."""

    payout_method: PayoutMethod = PayoutMethod.BANK_TRANSFER
    payout_account_ref: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Tokenised beneficiary reference from the payment service. Required unless "
            "the net refund is zero. Never send a raw IBAN."
        ),
    )
    payout_account_name: ShortText | None = None
    payout_account_last4: str | None = Field(default=None, min_length=4, max_length=4)
    note: LongText | None = None

    @model_validator(mode="after")
    def _account_required_for_transfer(self) -> PayDepositRequest:
        if self.payout_method is not PayoutMethod.OFFSET_ONLY and not self.payout_account_ref:
            raise ValueError("payout_account_ref is required for this payout method")
        return self


class ReopenReviewRequest(CommandModel):
    reason: LongText


class SettlementPreview(ApiModel):
    """Live projection of the settlement while damage review is open."""

    currency: str
    deposit_amount: Money
    total_deductions: Money
    net_refund_amount: Money
    tenant_liability_amount: Money
    open_disputes: int
    can_finalise: bool
    blocked_reason: str | None = None
    lines: list[DeductionLineResponse] = Field(default_factory=list)


class DeductionLineResponse(ApiModel):
    id: uuid.UUID | None = None
    damage_item_id: uuid.UUID | None = None
    category: DeductionCategory
    description: str
    amount: Money


class SettlementResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    status: SettlementStatus
    currency: str
    deposit_amount: Money
    total_deductions: Money
    net_refund_amount: Money
    tenant_liability_amount: Money
    computed_at: datetime | None
    finalised_at: datetime | None
    payout_method: PayoutMethod | None
    payout_account_name: str | None
    payout_account_last4: str | None
    payment_provider: str | None
    payment_reference: str | None
    payment_initiated_at: datetime | None
    payment_completed_at: datetime | None
    payment_attempts: int
    failure_code: str | None
    failure_reason: str | None
    owner_note: str | None
    deductions: list[DeductionLineResponse] = Field(default_factory=list)


class PaymentWebhookEvent(CommandModel):
    """Callback from the payment provider confirming or failing a payout."""

    event_id: str = Field(max_length=128)
    event_type: str = Field(max_length=64)
    payout_reference: str = Field(max_length=128)
    status: str = Field(max_length=32)
    failure_code: str | None = Field(default=None, max_length=64)
    failure_reason: str | None = Field(default=None, max_length=1000)
    occurred_at: datetime | None = None


SettlementPreview.model_rebuild()
