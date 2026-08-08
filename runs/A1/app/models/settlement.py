"""Deposit settlement and its payment legs (SRS O16).

A settlement has up to two legs:

* ``OWNER_REFUND``  — owner pays the tenant ``refund_fils`` ("Pay Deposit" in the BRD).
* ``TENANT_BALANCE`` — tenant pays the owner ``balance_due_fils`` when assessed damages
  exceeded the deposit.

Exactly one of the two amounts is ever non-zero. A leg whose amount is zero is satisfied on
creation. The settlement closes only when both legs are satisfied, and NOC issuance is gated
on that close.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, MoneyColumn, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum
from app.models.workflow import ActorType

if TYPE_CHECKING:
    from app.models.workflow import ExitWorkflow


class SettlementStatus(StrEnum):
    DRAFT = "DRAFT"
    PAYABLE = "PAYABLE"
    CLOSED = "CLOSED"
    VOID = "VOID"


class PaymentLeg(StrEnum):
    OWNER_REFUND = "OWNER_REFUND"
    TENANT_BALANCE = "TENANT_BALANCE"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class DepositSettlement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "deposit_settlements"
    __table_args__ = (
        sa.UniqueConstraint("workflow_id", name="uq_deposit_settlements_workflow"),
        sa.CheckConstraint("deposit_fils >= 0", name="ck_settlement_deposit_non_negative"),
        sa.CheckConstraint(
            "total_deductions_fils >= 0", name="ck_settlement_deductions_non_negative"
        ),
        sa.CheckConstraint("refund_fils >= 0", name="ck_settlement_refund_non_negative"),
        sa.CheckConstraint("balance_due_fils >= 0", name="ck_settlement_balance_non_negative"),
        # Only one side of the ledger can be non-zero.
        sa.CheckConstraint(
            "refund_fils = 0 OR balance_due_fils = 0", name="ck_settlement_single_sided"
        ),
        # The books must balance: refund - balance = deposit - deductions.
        sa.CheckConstraint(
            "refund_fils - balance_due_fils = deposit_fils - total_deductions_fils",
            name="ck_settlement_balances",
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exit_workflows.id", ondelete="CASCADE"), nullable=False
    )
    damage_report_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("damage_reports.id", ondelete="RESTRICT")
    )
    status: Mapped[SettlementStatus] = mapped_column(
        pg_enum(SettlementStatus, "settlement_status"),
        nullable=False,
        default=SettlementStatus.DRAFT,
    )
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="AED")

    deposit_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False)
    total_deductions_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False, default=0)
    refund_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False, default=0)
    balance_due_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False, default=0)

    refund_settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    balance_settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    computed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    approved_by_type: Mapped[ActorType | None] = mapped_column(pg_enum(ActorType, "actor_type"))
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    void_reason: Mapped[str | None] = mapped_column(sa.Text)

    #: Frozen copy of the line items the figures were derived from, for dispute handling.
    breakdown: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version}

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="settlement")
    payments: Mapped[list[PaymentTransaction]] = relationship(
        back_populates="settlement",
        cascade="all, delete-orphan",
        order_by="PaymentTransaction.created_at",
        lazy="selectin",
    )

    @property
    def refund_outstanding(self) -> bool:
        return self.refund_fils > 0 and self.refund_settled_at is None

    @property
    def balance_outstanding(self) -> bool:
        return self.balance_due_fils > 0 and self.balance_settled_at is None

    @property
    def is_fully_settled(self) -> bool:
        return not self.refund_outstanding and not self.balance_outstanding

    def leg_amount(self, leg: PaymentLeg) -> int:
        return self.refund_fils if leg is PaymentLeg.OWNER_REFUND else self.balance_due_fils


class PaymentTransaction(UUIDPrimaryKeyMixin, Base):
    """One attempt to move money for a settlement leg.

    ``idempotency_key`` is globally unique, so a retried "Pay Deposit" click — or a duplicate
    delivery from the payment provider — can never double-pay.
    """

    __tablename__ = "payment_transactions"
    __table_args__ = (
        sa.UniqueConstraint("idempotency_key", name="uq_payment_transactions_idempotency_key"),
        # At most one successful payment per settlement leg.
        sa.Index(
            "uq_payment_transactions_succeeded_leg",
            "settlement_id",
            "leg",
            unique=True,
            postgresql_where=sa.text("status = 'SUCCEEDED'"),
        ),
        sa.Index("ix_payment_transactions_workflow", "workflow_id"),
        sa.CheckConstraint("amount_fils >= 0", name="ck_payment_amount_non_negative"),
    )

    settlement_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("deposit_settlements.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exit_workflows.id", ondelete="CASCADE"), nullable=False
    )
    leg: Mapped[PaymentLeg] = mapped_column(pg_enum(PaymentLeg, "payment_leg"), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        pg_enum(PaymentStatus, "payment_status"), nullable=False, default=PaymentStatus.PENDING
    )
    amount_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False)
    currency: Mapped[str] = mapped_column(sa.String(3), nullable=False, default="AED")
    idempotency_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    provider: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="internal")
    provider_reference: Mapped[str | None] = mapped_column(sa.String(128))
    failure_reason: Mapped[str | None] = mapped_column(sa.Text)
    initiated_by_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False
    )
    initiated_by_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    settlement: Mapped[DepositSettlement] = relationship(back_populates="payments")
