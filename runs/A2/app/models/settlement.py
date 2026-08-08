"""Deposit settlement (SRS O16, T13 step 9).

The settlement is a frozen snapshot of the arithmetic at the moment the owner finalises
it: deposit, every deduction line, the net refund and any residual tenant liability.
Snapshotting rather than recomputing on read means a NOC issued today still reconciles
against the ledger in seven years, even if the damage items are later corrected.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import MONEY, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column
from app.domain.enums import DeductionCategory, PayoutMethod, SettlementStatus

if TYPE_CHECKING:
    from app.models.exit_workflow import ExitWorkflow


class Settlement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "exit_workflow_settlement"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    status: Mapped[SettlementStatus] = mapped_column(
        enum_column(SettlementStatus), nullable=False, default=SettlementStatus.DRAFT
    )
    version: Mapped[int] = mapped_column(nullable=False, default=1, server_default=text("1"))

    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="AED", server_default=text("'AED'")
    )
    deposit_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    total_deductions: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    #: deposit - deductions, floored at zero (SRS: "deposit minus damage").
    net_refund_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    #: The excess when deductions exceed the deposit; pursued outside this module.
    tenant_liability_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )

    computed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finalised_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finalised_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    owner_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------------- payout
    payout_method: Mapped[PayoutMethod | None] = mapped_column(
        enum_column(PayoutMethod), nullable=True
    )
    #: Tokenised beneficiary reference held by the payment service. Never raw IBANs:
    #: this module must not become a store of payment instruments.
    payout_account_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payout_account_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    payout_account_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    payment_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Sent to the provider so a retried 'Pay Deposit' click cannot double-pay.
    payment_idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    payment_initiated_at: Mapped[datetime | None] = mapped_column(nullable=True)
    payment_initiated_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    payment_completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    payment_attempts: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="settlement")
    deductions: Mapped[list[SettlementDeduction]] = relationship(
        back_populates="settlement",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SettlementDeduction.created_at",
    )

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        Index("ix_exit_workflow_settlement_status", "status"),
        Index(
            "ix_exit_workflow_settlement_payment_reference",
            "payment_reference",
            postgresql_where=text("payment_reference IS NOT NULL"),
        ),
        CheckConstraint("deposit_amount >= 0", name="deposit_non_negative"),
        CheckConstraint("total_deductions >= 0", name="deductions_non_negative"),
        CheckConstraint("net_refund_amount >= 0", name="refund_non_negative"),
        CheckConstraint("tenant_liability_amount >= 0", name="liability_non_negative"),
        # The core O16 identity, enforced by the database rather than trusted from code.
        CheckConstraint(
            "net_refund_amount = GREATEST(deposit_amount - total_deductions, 0)",
            name="refund_equals_deposit_minus_deductions",
        ),
        CheckConstraint(
            "tenant_liability_amount = GREATEST(total_deductions - deposit_amount, 0)",
            name="liability_equals_excess_deductions",
        ),
        CheckConstraint("payment_attempts >= 0", name="payment_attempts_non_negative"),
        {"comment": "Security deposit settlement for an exit workflow (SRS O16)."},
    )

    @property
    def requires_payout(self) -> bool:
        """False when the refund nets to zero -- there is nothing to transfer."""
        return self.net_refund_amount > Decimal("0.00")


class SettlementDeduction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One frozen deduction line.

    Lines sourced from the inspection keep a pointer back to the damage item; owners may
    also add non-damage lines (unpaid rent, utilities) that the inspection cannot know
    about.
    """

    __tablename__ = "exit_workflow_settlement_deduction"

    settlement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow_settlement.id", ondelete="CASCADE"), nullable=False
    )
    damage_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exit_workflow_damage_item.id", ondelete="SET NULL"), nullable=True
    )
    category: Mapped[DeductionCategory] = mapped_column(
        enum_column(DeductionCategory), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    settlement: Mapped[Settlement] = relationship(back_populates="deductions")

    __table_args__ = (
        Index("ix_exit_workflow_settlement_deduction_stl", "settlement_id"),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        CheckConstraint("char_length(description) > 0", name="description_not_blank"),
        {"comment": "Frozen deduction lines making up a settlement."},
    )
