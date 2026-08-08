"""Settlement, payment, NOC and contract-eligibility models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.noc import ExitNOC
from app.models.settlement import (
    DepositSettlement,
    PaymentLeg,
    PaymentStatus,
    PaymentTransaction,
    SettlementStatus,
)
from app.models.workflow import ActorType
from app.schemas.common import MoneyOut


# --- requests ---------------------------------------------------------------------------


class PayRequest(BaseModel):
    """The BRD's "Pay Deposit" action. Send the same idempotency key when retrying."""

    model_config = ConfigDict(extra="forbid")

    leg: PaymentLeg = PaymentLeg.OWNER_REFUND
    #: Optional in the body; the ``Idempotency-Key`` header is used when omitted.
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class CreateContractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_number: Annotated[str, Field(min_length=1, max_length=64)]
    property_id: uuid.UUID
    tenant_id: uuid.UUID
    start_date: date
    end_date: date
    security_deposit_fils: Annotated[int, Field(ge=0)]
    annual_rent_fils: Annotated[int, Field(ge=0)]


# --- responses --------------------------------------------------------------------------


class PaymentOut(BaseModel):
    id: uuid.UUID
    leg: PaymentLeg
    status: PaymentStatus
    amount: MoneyOut
    provider: str
    provider_reference: str | None
    idempotency_key: str
    initiated_by_type: ActorType
    created_at: datetime
    completed_at: datetime | None
    failure_reason: str | None

    @classmethod
    def of(cls, payment: PaymentTransaction) -> PaymentOut:
        return cls(
            id=payment.id,
            leg=payment.leg,
            status=payment.status,
            amount=MoneyOut.of(payment.amount_fils, payment.currency),
            provider=payment.provider,
            provider_reference=payment.provider_reference,
            idempotency_key=payment.idempotency_key,
            initiated_by_type=payment.initiated_by_type,
            created_at=payment.created_at,
            completed_at=payment.completed_at,
            failure_reason=payment.failure_reason,
        )


class SettlementOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    damage_report_id: uuid.UUID | None
    status: SettlementStatus
    deposit: MoneyOut
    total_deductions: MoneyOut
    refund: MoneyOut
    balance_due: MoneyOut
    #: True when assessed damages exceeded the deposit and the tenant owes the difference.
    tenant_owes_balance: bool
    refund_settled_at: datetime | None
    balance_settled_at: datetime | None
    computed_at: datetime
    approved_at: datetime | None
    closed_at: datetime | None
    void_reason: str | None
    breakdown: dict[str, Any]
    payments: list[PaymentOut]

    @classmethod
    def of(cls, settlement: DepositSettlement) -> SettlementOut:
        currency = settlement.currency
        return cls(
            id=settlement.id,
            workflow_id=settlement.workflow_id,
            damage_report_id=settlement.damage_report_id,
            status=settlement.status,
            deposit=MoneyOut.of(settlement.deposit_fils, currency),
            total_deductions=MoneyOut.of(settlement.total_deductions_fils, currency),
            refund=MoneyOut.of(settlement.refund_fils, currency),
            balance_due=MoneyOut.of(settlement.balance_due_fils, currency),
            tenant_owes_balance=settlement.balance_due_fils > 0,
            refund_settled_at=settlement.refund_settled_at,
            balance_settled_at=settlement.balance_settled_at,
            computed_at=settlement.computed_at,
            approved_at=settlement.approved_at,
            closed_at=settlement.closed_at,
            void_reason=settlement.void_reason,
            breakdown=settlement.breakdown,
            payments=[PaymentOut.of(p) for p in settlement.payments],
        )


class NOCOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    noc_number: str
    issued_at: datetime
    content_sha256: str
    byte_size: int
    download_count: int
    first_downloaded_at: datetime | None
    last_downloaded_at: datetime | None
    snapshot: dict[str, Any]
    download_url: str

    @classmethod
    def of(cls, noc: ExitNOC, *, download_url: str) -> NOCOut:
        return cls(
            id=noc.id,
            workflow_id=noc.workflow_id,
            noc_number=noc.noc_number,
            issued_at=noc.issued_at,
            content_sha256=noc.content_sha256,
            byte_size=noc.byte_size,
            download_count=noc.download_count,
            first_downloaded_at=noc.first_downloaded_at,
            last_downloaded_at=noc.last_downloaded_at,
            snapshot=noc.snapshot,
            download_url=download_url,
        )


class BlockerOut(BaseModel):
    scope: str
    workflow_id: uuid.UUID
    reference: str
    state: str
    message: str


class EligibilityOut(BaseModel):
    """BR-1 probe result. ``warnings`` is what the portal should display."""

    allowed: bool
    blockers: list[BlockerOut]
    warnings: list[str]


class ContractOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    contract_number: str
    property_id: uuid.UUID
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    status: str
    start_date: date
    end_date: date
    security_deposit_fils: int
    annual_rent_fils: int
