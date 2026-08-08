"""Third-party inspection (SRS O15) and the damage assessment it produces (O16)."""

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
from app.domain.enums import (
    DamageSeverity,
    DeductionCategory,
    DisputeStatus,
    InspectionStatus,
    PropertyCondition,
)

if TYPE_CHECKING:
    from app.models.exit_workflow import ExitWorkflow


class Inspection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One inspection engagement with a registered agency.

    One row per workflow. A re-inspection reuses the row and resets its status, so the
    workflow always has exactly one current inspection; superseded reports remain
    attached as documents and in the audit trail.
    """

    __tablename__ = "exit_workflow_inspection"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    agency_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agency_email: Mapped[str] = mapped_column(String(320), nullable=False)

    status: Mapped[InspectionStatus] = mapped_column(
        enum_column(InspectionStatus), nullable=False, default=InspectionStatus.REQUESTED
    )
    round_number: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default=text("1"),
        comment="Incremented on re-inspection",
    )

    requested_at: Mapped[datetime] = mapped_column(nullable=False)
    agency_notified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    slots_proposed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    scheduled_start: Mapped[datetime | None] = mapped_column(nullable=True)
    scheduled_end: Mapped[datetime | None] = mapped_column(nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    scheduled_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    conducted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reported_at: Mapped[datetime | None] = mapped_column(nullable=True)

    inspector_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inspector_licence_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    overall_condition: Mapped[PropertyCondition | None] = mapped_column(
        enum_column(PropertyCondition), nullable=True
    )
    report_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("exit_workflow_document.id", ondelete="SET NULL"), nullable=True
    )
    #: Sum of the agency's estimates. The owner's approved figures live on the settlement.
    assessed_total: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="inspection")
    slots: Mapped[list[InspectionSlot]] = relationship(
        back_populates="inspection",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="InspectionSlot.starts_at",
    )
    damage_items: Mapped[list[DamageItem]] = relationship(
        back_populates="inspection",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="DamageItem.created_at",
    )

    __table_args__ = (
        CheckConstraint(
            "scheduled_end IS NULL OR scheduled_start IS NULL OR scheduled_end > scheduled_start",
            name="schedule_window_ordered",
        ),
        CheckConstraint("round_number >= 1", name="round_number_positive"),
        {"comment": "Third-party inspection engagement (SRS O15)."},
    )

    @property
    def selected_slot(self) -> InspectionSlot | None:
        return next((s for s in self.slots if s.is_selected), None)


class InspectionSlot(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An appointment window offered by the agency (O15: "agency responds with dates")."""

    __tablename__ = "exit_workflow_inspection_slot"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow_inspection.id", ondelete="CASCADE"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(nullable=False, default=1, server_default=text("1"))
    starts_at: Mapped[datetime] = mapped_column(nullable=False)
    ends_at: Mapped[datetime] = mapped_column(nullable=False)
    is_selected: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    selected_at: Mapped[datetime | None] = mapped_column(nullable=True)
    selected_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    inspection: Mapped[Inspection] = relationship(back_populates="slots")

    __table_args__ = (
        # At most one selected slot per inspection.
        Index(
            "uq_exit_workflow_inspection_slot_selected",
            "inspection_id",
            unique=True,
            postgresql_where=text("is_selected"),
        ),
        Index("ix_exit_workflow_inspection_slot_insp", "inspection_id", "starts_at"),
        CheckConstraint("ends_at > starts_at", name="slot_window_ordered"),
        {"comment": "Inspection appointment windows offered by the agency."},
    )


class DamageItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single line of the agency's damage report (SRS O16).

    ``estimated_cost`` is the agency's figure and is immutable once reported.
    ``approved_cost`` is what the owner accepts during damage review (T13 step 8) and is
    what actually reaches the settlement. Keeping both preserves the evidence chain when
    an owner waives or reduces a charge.
    """

    __tablename__ = "exit_workflow_damage_item"

    inspection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow_inspection.id", ondelete="CASCADE"), nullable=False
    )
    round_number: Mapped[int] = mapped_column(nullable=False, default=1, server_default=text("1"))

    category: Mapped[DeductionCategory] = mapped_column(
        enum_column(DeductionCategory), nullable=False, default=DeductionCategory.DAMAGE
    )
    severity: Mapped[DamageSeverity] = mapped_column(
        enum_column(DamageSeverity), nullable=False, default=DamageSeverity.MINOR
    )
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    estimated_cost: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    approved_cost: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    adjusted_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    adjusted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    adjustment_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: True when the agency judges the damage beyond fair wear and tear.
    tenant_liable: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )

    dispute_status: Mapped[DisputeStatus] = mapped_column(
        enum_column(DisputeStatus), nullable=False, default=DisputeStatus.NONE
    )
    dispute_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    disputed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    disputed_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    dispute_resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispute_resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    dispute_resolved_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    #: Ids of ``ExitDocument`` rows of type DAMAGE_PHOTO evidencing this item.
    photo_document_ids: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    inspection: Mapped[Inspection] = relationship(back_populates="damage_items")

    __table_args__ = (
        Index("ix_exit_workflow_damage_item_insp", "inspection_id", "round_number"),
        Index(
            "ix_exit_workflow_damage_item_disputed",
            "inspection_id",
            postgresql_where=text("dispute_status = 'RAISED'"),
        ),
        CheckConstraint("estimated_cost >= 0", name="estimated_cost_non_negative"),
        CheckConstraint(
            "approved_cost IS NULL OR approved_cost >= 0", name="approved_cost_non_negative"
        ),
        CheckConstraint("char_length(description) > 0", name="description_not_blank"),
        {"comment": "Damage report line items uploaded by the inspection agency (SRS O16)."},
    )

    @property
    def chargeable_amount(self) -> Decimal:
        """What this item contributes to the deduction total.

        A dispute resolved in the tenant's favour is expressed by the resolver writing
        ``approved_cost`` (0 for a full waiver, a lower figure for a partial one) rather
        than by inferring an amount from the dispute status -- so a partly-upheld dispute
        is representable.
        """
        if not self.tenant_liable:
            return Decimal("0.00")
        return self.approved_cost if self.approved_cost is not None else self.estimated_cost

    @property
    def is_open_dispute(self) -> bool:
        return self.dispute_status is DisputeStatus.RAISED
