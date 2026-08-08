"""O16 — the digital Exit NOC certificate."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from exit_workflow.models.base import (
    Base,
    MoneyType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

if TYPE_CHECKING:  # pragma: no cover
    from exit_workflow.models.workflow import ExitWorkflow


class ExitNoc(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A No Objection Certificate, auto-generated once the deposit is settled.

    The certificate body is rendered once and stored; the row keeps a SHA-256
    of the exact bytes so a presented PDF can be proven authentic years later
    (A3: 7-year audit retention).
    """

    __tablename__ = "exit_noc"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("settlement.id", ondelete="SET NULL")
    )

    noc_number: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    verification_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    issued_at: Mapped[datetime] = mapped_column(nullable=False)

    # --- certificate facts, frozen at issuance ----------------------------
    property_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    property_reference: Mapped[str | None] = mapped_column(String(64))
    property_address: Mapped[str | None] = mapped_column(String(512))
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_name: Mapped[str | None] = mapped_column(String(255))
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    owner_name: Mapped[str | None] = mapped_column(String(255))
    contract_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    move_out_date: Mapped[date] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    security_deposit_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    total_deduction_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    refund_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)

    # --- stored artefact ---------------------------------------------------
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False, default="application/pdf")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_downloaded_at: Mapped[datetime | None] = mapped_column()
    last_downloaded_at: Mapped[datetime | None] = mapped_column()

    revoked_at: Mapped[datetime | None] = mapped_column()
    revoked_by: Mapped[uuid.UUID | None] = mapped_column()
    revocation_reason: Mapped[str | None] = mapped_column(Text)

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="noc")

    __table_args__ = (
        CheckConstraint("char_length(content_sha256) = 64", name="content_hash_is_sha256"),
        CheckConstraint("size_bytes > 0", name="noc_size_positive"),
    )

    @property
    def is_valid(self) -> bool:
        return self.revoked_at is None
