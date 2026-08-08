"""Third-party inspection scheduling and damage reporting (SRS O15, O16).

Mirrors the Appendix B sequence: owner approves exit -> workflow ID generated -> agency
notified with property details -> agency responds with available dates -> owner/tenant select
a date -> inspection occurs -> report uploaded with photos.

A workflow may have more than one assignment (``attempt``) when a report is disputed and a
re-inspection is ordered; the latest attempt is the operative one.
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
    from app.models.catalog import InspectionAgency
    from app.models.workflow import ExitWorkflow


class AssignmentStatus(StrEnum):
    REQUESTED = "REQUESTED"
    SLOTS_PROPOSED = "SLOTS_PROPOSED"
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class DamageSeverity(StrEnum):
    MINOR = "MINOR"
    MODERATE = "MODERATE"
    MAJOR = "MAJOR"
    FAIR_WEAR_AND_TEAR = "FAIR_WEAR_AND_TEAR"


class InspectionAssignment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "inspection_assignments"
    __table_args__ = (
        sa.UniqueConstraint("workflow_id", "attempt", name="uq_inspection_assignment_attempt"),
        sa.Index("ix_inspection_assignments_agency_status", "agency_id", "status"),
        sa.CheckConstraint("attempt >= 1", name="ck_inspection_assignments_attempt_positive"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exit_workflows.id", ondelete="CASCADE"), nullable=False
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("inspection_agencies.id", ondelete="RESTRICT"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    status: Mapped[AssignmentStatus] = mapped_column(
        pg_enum(AssignmentStatus, "assignment_status"),
        nullable=False,
        default=AssignmentStatus.REQUESTED,
    )
    requested_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    notified_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    scheduled_start: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    scheduled_end: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    cancelled_reason: Mapped[str | None] = mapped_column(sa.Text)
    instructions: Mapped[str | None] = mapped_column(sa.Text)

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="assignments")
    agency: Mapped[InspectionAgency] = relationship(lazy="selectin")
    slots: Mapped[list[InspectionSlot]] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        order_by="InspectionSlot.starts_at",
        lazy="selectin",
    )
    report: Mapped[DamageReport | None] = relationship(
        back_populates="assignment", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def selected_slot(self) -> InspectionSlot | None:
        return next((slot for slot in self.slots if slot.is_selected), None)


class InspectionSlot(UUIDPrimaryKeyMixin, Base):
    """An appointment window offered by the agency."""

    __tablename__ = "inspection_slots"
    __table_args__ = (
        sa.CheckConstraint("ends_at > starts_at", name="ck_inspection_slots_time_order"),
        sa.UniqueConstraint(
            "assignment_id", "starts_at", "ends_at", name="uq_inspection_slot_window"
        ),
        # At most one selected slot per assignment.
        sa.Index(
            "uq_inspection_slots_selected",
            "assignment_id",
            unique=True,
            postgresql_where=sa.text("is_selected"),
        ),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("inspection_assignments.id", ondelete="CASCADE"), nullable=False
    )
    starts_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    is_selected: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    selected_by_type: Mapped[ActorType | None] = mapped_column(pg_enum(ActorType, "actor_type"))
    selected_by_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    selected_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    proposed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    assignment: Mapped[InspectionAssignment] = relationship(back_populates="slots")


class DamageReport(UUIDPrimaryKeyMixin, Base):
    """An immutable damage report uploaded by the agency (O16).

    ``total_deductions_fils`` is a stored aggregate of the line items, recomputed by the
    service on write; the DB check keeps it non-negative.
    """

    __tablename__ = "damage_reports"
    __table_args__ = (
        sa.UniqueConstraint("assignment_id", name="uq_damage_reports_assignment"),
        sa.CheckConstraint(
            "total_deductions_fils >= 0", name="ck_damage_reports_total_non_negative"
        ),
        sa.Index("ix_damage_reports_workflow", "workflow_id"),
    )

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("inspection_assignments.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exit_workflows.id", ondelete="CASCADE"), nullable=False
    )
    agency_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("inspection_agencies.id", ondelete="RESTRICT"), nullable=False
    )
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    inspector_name: Mapped[str | None] = mapped_column(sa.String(200))
    inspected_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    total_deductions_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False, default=0)
    #: Report-level evidence: ``[{"storage_key": ..., "caption": ...}, ...]``.
    photos: Mapped[list[dict[str, Any]]] = mapped_column(nullable=False, default=list)

    assignment: Mapped[InspectionAssignment] = relationship(back_populates="report")
    line_items: Mapped[list[DamageLineItem]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="selectin"
    )


class DamageLineItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "damage_line_items"
    __table_args__ = (
        sa.CheckConstraint("amount_fils >= 0", name="ck_damage_line_items_amount_non_negative"),
        sa.Index("ix_damage_line_items_report", "report_id"),
    )

    report_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("damage_reports.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    location: Mapped[str | None] = mapped_column(sa.String(120))
    severity: Mapped[DamageSeverity] = mapped_column(
        pg_enum(DamageSeverity, "damage_severity"), nullable=False
    )
    amount_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False)
    photos: Mapped[list[dict[str, Any]]] = mapped_column(nullable=False, default=list)

    report: Mapped[DamageReport] = relationship(back_populates="line_items")
