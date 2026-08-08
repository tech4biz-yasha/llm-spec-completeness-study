"""O16 — deposit settlement and the payment ledger (PostgreSQL, per §15)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from exit_workflow.domain.enums import PaymentStatus, PayoutMethod, SettlementStatus
from exit_workflow.models.base import (
    Base,
    MoneyType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)

if TYPE_CHECKING:  # pragma: no cover
    from exit_workflow.models.workflow import ExitWorkflow


class Settlement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The deposit calculation for one exit workflow.

    Exactly one per workflow. Amounts are frozen when the owner finalises the
    deduction and are never recomputed after payment.
    """

    __tablename__ = "settlement"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    damage_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("damage_report.id", ondelete="SET NULL")
    )

    status: Mapped[SettlementStatus] = mapped_column(
        pg_enum(SettlementStatus, "settlement_status"),
        nullable=False,
        default=SettlementStatus.PENDING,
        index=True,
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="AED")
    security_deposit_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    total_deduction_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False, default=0)
    #: deposit − deductions, floored at zero.
    refund_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False, default=0)
    #: Shortfall when damage exceeded the deposit; pursued outside this module.
    balance_due_from_tenant: Mapped[Decimal] = mapped_column(MoneyType, nullable=False, default=0)

    computed_at: Mapped[datetime] = mapped_column(nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column()
    finalized_by: Mapped[uuid.UUID | None] = mapped_column()
    adjustment_reason: Mapped[str | None] = mapped_column(Text)

    payout_method: Mapped[PayoutMethod] = mapped_column(
        pg_enum(PayoutMethod, "payout_method"), nullable=False, default=PayoutMethod.BANK_TRANSFER
    )
    #: Tokenised destination from the payment service — never a raw IBAN.
    payout_destination_token: Mapped[str | None] = mapped_column(String(128))
    payout_destination_masked: Mapped[str | None] = mapped_column(String(64))

    paid_at: Mapped[datetime | None] = mapped_column()
    paid_by: Mapped[uuid.UUID | None] = mapped_column()
    payment_reference: Mapped[str | None] = mapped_column(String(128))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version_id}

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="settlement")
    transactions: Mapped[list[PaymentTransaction]] = relationship(
        back_populates="settlement",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PaymentTransaction.created_at",
    )

    __table_args__ = (
        CheckConstraint("security_deposit_amount >= 0", name="deposit_non_negative"),
        CheckConstraint("total_deduction_amount >= 0", name="deduction_non_negative"),
        CheckConstraint("refund_amount >= 0", name="refund_non_negative"),
        CheckConstraint("balance_due_from_tenant >= 0", name="balance_non_negative"),
        # The identity that defines a settlement: what is refunded plus what is
        # deducted plus any unrecoverable shortfall reconciles to the deposit.
        CheckConstraint(
            "refund_amount = security_deposit_amount - total_deduction_amount "
            "+ balance_due_from_tenant",
            name="amounts_reconcile",
        ),
        CheckConstraint(
            "refund_amount = 0 OR balance_due_from_tenant = 0",
            name="refund_and_balance_mutually_exclusive",
        ),
    )

    @property
    def requires_payout(self) -> bool:
        return self.refund_amount > 0


class PaymentTransaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only attempt log for the deposit payout.

    One row per gateway call. The idempotency key is unique so that a retried
    "Pay Deposit" click can never produce a second transfer.
    """

    __tablename__ = "payment_transaction"

    settlement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("settlement.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    status: Mapped[PaymentStatus] = mapped_column(
        pg_enum(PaymentStatus, "payment_status"), nullable=False, default=PaymentStatus.PENDING
    )
    amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False, default="REFUND_TO_TENANT")
    gateway: Mapped[str] = mapped_column(String(64), nullable=False)
    gateway_reference: Mapped[str | None] = mapped_column(String(128), index=True)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    initiated_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    initiated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column()
    #: Redacted gateway response retained for reconciliation.
    gateway_response: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    settlement: Mapped[Settlement] = relationship(back_populates="transactions")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_payment_transaction_settlement_status", "settlement_id", "status"),
    )
