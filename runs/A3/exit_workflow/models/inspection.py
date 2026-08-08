"""O15 inspection workflow and the damage report it produces (O16 input)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from exit_workflow.domain.enums import (
    INSPECTION_TERMINAL_STATUSES,
    DamageCategory,
    DamageReportStatus,
    DamageSeverity,
    InspectionStatus,
    SlotStatus,
)
from exit_workflow.models.base import (
    Base,
    MoneyType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)

if TYPE_CHECKING:  # pragma: no cover
    from exit_workflow.models.document import Document
    from exit_workflow.models.workflow import ExitWorkflow

_ACTIVE_INSPECTION_PREDICATE = "status NOT IN (" + ", ".join(
    f"'{s.value}'" for s in sorted(INSPECTION_TERMINAL_STATUSES)
) + ")"


class Inspection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One engagement of a registered inspection agency for an exit."""

    __tablename__ = "inspection"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 1-based; a re-inspection creates attempt 2 and so on.
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reference: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)

    agency_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    agency_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agency_email: Mapped[str] = mapped_column(String(320), nullable=False)

    status: Mapped[InspectionStatus] = mapped_column(
        pg_enum(InspectionStatus, "inspection_status"),
        nullable=False,
        default=InspectionStatus.REQUESTED,
        index=True,
    )
    request_notes: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(nullable=False)

    scheduled_slot_id: Mapped[uuid.UUID | None] = mapped_column()
    scheduled_start: Mapped[datetime | None] = mapped_column()
    scheduled_end: Mapped[datetime | None] = mapped_column()
    scheduled_by: Mapped[uuid.UUID | None] = mapped_column()
    scheduled_at: Mapped[datetime | None] = mapped_column()

    conducted_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    cancelled_at: Mapped[datetime | None] = mapped_column()
    cancellation_reason: Mapped[str | None] = mapped_column(Text)

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="inspections")
    slots: Mapped[list[InspectionSlot]] = relationship(
        back_populates="inspection",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InspectionSlot.starts_at",
    )
    damage_report: Mapped[DamageReport | None] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    __table_args__ = (
        # At most one live inspection per exit workflow.
        Index(
            "uq_inspection_active_per_workflow",
            "workflow_id",
            unique=True,
            postgresql_where=text(_ACTIVE_INSPECTION_PREDICATE),
        ),
        Index("uq_inspection_workflow_attempt", "workflow_id", "attempt_no", unique=True),
        CheckConstraint("attempt_no >= 1", name="attempt_positive"),
        CheckConstraint(
            "scheduled_end IS NULL OR scheduled_start IS NULL OR scheduled_end > scheduled_start",
            name="schedule_window_valid",
        ),
    )

    @property
    def is_active(self) -> bool:
        return self.status not in INSPECTION_TERMINAL_STATUSES


class InspectionSlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An availability window proposed by the agency (Appendix B O15)."""

    __tablename__ = "inspection_slot"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspection.id", ondelete="CASCADE"), nullable=False, index=True
    )
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[SlotStatus] = mapped_column(
        pg_enum(SlotStatus, "slot_status"), nullable=False, default=SlotStatus.PROPOSED
    )
    proposed_at: Mapped[datetime] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(String(512))

    inspection: Mapped[Inspection] = relationship(back_populates="slots")

    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="slot_window_valid"),
        Index("ix_inspection_slot_inspection_status", "inspection_id", "status"),
    )


class DamageReport(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The agency's assessment: the sole input to the O16 deduction."""

    __tablename__ = "damage_report"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("inspection.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    #: Denormalised so the report can be fetched without joining the inspection.
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[DamageReportStatus] = mapped_column(
        pg_enum(DamageReportStatus, "damage_report_status"),
        nullable=False,
        default=DamageReportStatus.SUBMITTED,
    )
    summary: Mapped[str | None] = mapped_column(Text)
    inspected_at: Mapped[datetime] = mapped_column(nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(nullable=False)
    submitted_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    inspector_name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="AED")
    #: Sum of tenant-liable line items, computed server-side.
    assessed_total: Mapped[Decimal] = mapped_column(MoneyType, nullable=False, default=0)

    # --- tenant review (T13 step 7) ---------------------------------------
    tenant_reviewed_at: Mapped[datetime | None] = mapped_column()
    tenant_review_note: Mapped[str | None] = mapped_column(Text)
    dispute_reason: Mapped[str | None] = mapped_column(Text)
    dispute_resolved_at: Mapped[datetime | None] = mapped_column()
    dispute_resolved_by: Mapped[uuid.UUID | None] = mapped_column()
    dispute_resolution_note: Mapped[str | None] = mapped_column(Text)

    # --- owner finalisation ------------------------------------------------
    finalized_total: Mapped[Decimal | None] = mapped_column(MoneyType)
    finalized_at: Mapped[datetime | None] = mapped_column()
    finalized_by: Mapped[uuid.UUID | None] = mapped_column()
    adjustment_reason: Mapped[str | None] = mapped_column(Text)

    inspection: Mapped[Inspection] = relationship(back_populates="damage_report")
    line_items: Mapped[list[DamageLineItem]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("assessed_total >= 0", name="assessed_total_non_negative"),
        CheckConstraint(
            "finalized_total IS NULL OR finalized_total <= assessed_total",
            name="finalized_not_above_assessed",
        ),
        CheckConstraint(
            "finalized_total IS NULL OR finalized_total >= 0", name="finalized_non_negative"
        ),
    )

    @property
    def is_disputed(self) -> bool:
        return self.status is DamageReportStatus.DISPUTED


class DamageLineItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "damage_line_item"

    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("damage_report.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[DamageCategory] = mapped_column(
        pg_enum(DamageCategory, "damage_category"), nullable=False
    )
    severity: Mapped[DamageSeverity] = mapped_column(
        pg_enum(DamageSeverity, "damage_severity"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    location: Mapped[str | None] = mapped_column(String(255))
    assessed_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    #: Fair wear and tear is recorded but not charged to the tenant.
    tenant_liable: Mapped[bool] = mapped_column(nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    report: Mapped[DamageReport] = relationship(back_populates="line_items")
    photos: Mapped[list[Document]] = relationship(
        back_populates="damage_line_item", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("assessed_amount >= 0", name="line_amount_non_negative"),
    )
