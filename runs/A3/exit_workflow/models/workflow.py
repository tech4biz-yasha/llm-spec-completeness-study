"""The exit workflow aggregate root."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from exit_workflow.domain.enums import (
    ExitReason,
    ExitWorkflowStatus,
    TERMINAL_STATUSES,
)
from exit_workflow.models.base import (
    Base,
    MoneyType,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    pg_enum,
)

if TYPE_CHECKING:  # pragma: no cover
    from exit_workflow.models.audit import WorkflowTransition
    from exit_workflow.models.document import Document
    from exit_workflow.models.inspection import Inspection
    from exit_workflow.models.noc import ExitNoc
    from exit_workflow.models.settlement import Settlement

_TERMINAL_SQL = ", ".join(f"'{s.value}'" for s in sorted(TERMINAL_STATUSES))
#: BR-1 predicate: a workflow holds the contract lock until it is COMPLETE
#: (or was cancelled/rejected, in which case it never completes).
ACTIVE_WORKFLOW_PREDICATE = f"status NOT IN ({_TERMINAL_SQL})"


class ExitWorkflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """T13 — a tenant's exit from one contract, start to finish."""

    __tablename__ = "exit_workflow"

    #: The human-facing "Workflow ID" of T13 step 4, quoted in every email.
    reference: Mapped[str] = mapped_column(String(24), unique=True, nullable=False)

    # --- parties and subject (snapshot from the Property service at
    # initiation; the tenant never supplies these) --------------------------
    contract_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    property_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)

    property_reference: Mapped[str | None] = mapped_column(String(64))
    property_address: Mapped[str | None] = mapped_column(String(512))
    tenant_name: Mapped[str | None] = mapped_column(String(255))
    tenant_email: Mapped[str | None] = mapped_column(String(320))
    owner_name: Mapped[str | None] = mapped_column(String(255))
    owner_email: Mapped[str | None] = mapped_column(String(320))

    # --- request ------------------------------------------------------------
    status: Mapped[ExitWorkflowStatus] = mapped_column(
        pg_enum(ExitWorkflowStatus, "exit_workflow_status"),
        nullable=False,
        default=ExitWorkflowStatus.INITIATED,
        index=True,
    )
    move_out_date: Mapped[date] = mapped_column(nullable=False, index=True)
    reason: Mapped[ExitReason] = mapped_column(pg_enum(ExitReason, "exit_reason"), nullable=False)
    reason_details: Mapped[str | None] = mapped_column(Text)

    # --- money (authoritative deposit copied from the contract) ------------
    security_deposit_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="AED")

    # --- denormalised progress markers (drive the 10-step view) ------------
    documents_uploaded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    initiated_at: Mapped[datetime] = mapped_column(nullable=False)
    initiated_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column()
    owner_decision_at: Mapped[datetime | None] = mapped_column()
    owner_decision_by: Mapped[uuid.UUID | None] = mapped_column()
    owner_rejection_reason: Mapped[str | None] = mapped_column(Text)
    inspection_requested_at: Mapped[datetime | None] = mapped_column()
    inspection_scheduled_at: Mapped[datetime | None] = mapped_column()
    inspection_completed_at: Mapped[datetime | None] = mapped_column()
    damage_review_completed_at: Mapped[datetime | None] = mapped_column()
    settlement_completed_at: Mapped[datetime | None] = mapped_column()
    noc_issued_at: Mapped[datetime | None] = mapped_column()
    noc_first_downloaded_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()
    closed_at: Mapped[datetime | None] = mapped_column()
    closure_reason: Mapped[str | None] = mapped_column(Text)
    closed_by: Mapped[uuid.UUID | None] = mapped_column()

    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    #: Optimistic lock; every service write also takes a row lock, so this is
    #: belt-and-braces against a stale in-memory instance.
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version_id}

    documents: Mapped[list[Document]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="selectin"
    )
    inspections: Mapped[list[Inspection]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Inspection.attempt_no",
    )
    settlement: Mapped[Settlement | None] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    noc: Mapped[ExitNoc | None] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    transitions: Mapped[list[WorkflowTransition]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="noload",
        order_by="WorkflowTransition.occurred_at",
    )

    __table_args__ = (
        # BR-1, enforced by the database rather than by application checks:
        # at most one live exit workflow per property and per tenant. Two
        # concurrent initiations cannot both win.
        Index(
            "uq_exit_workflow_active_property",
            "property_id",
            unique=True,
            postgresql_where=text(ACTIVE_WORKFLOW_PREDICATE),
        ),
        Index(
            "uq_exit_workflow_active_tenant",
            "tenant_id",
            unique=True,
            postgresql_where=text(ACTIVE_WORKFLOW_PREDICATE),
        ),
        Index(
            "uq_exit_workflow_active_contract",
            "contract_id",
            unique=True,
            postgresql_where=text(ACTIVE_WORKFLOW_PREDICATE),
        ),
        Index("ix_exit_workflow_owner_status", "owner_id", "status"),
        Index("ix_exit_workflow_tenant_status", "tenant_id", "status"),
        CheckConstraint("security_deposit_amount >= 0", name="deposit_non_negative"),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        CheckConstraint(
            "reason <> 'OTHER' OR reason_details IS NOT NULL",
            name="other_reason_requires_details",
        ),
    )

    # --- convenience -------------------------------------------------------
    @property
    def is_active(self) -> bool:
        return self.status not in TERMINAL_STATUSES

    @property
    def is_complete(self) -> bool:
        return self.status is ExitWorkflowStatus.COMPLETED

    def party_ids(self) -> set[uuid.UUID]:
        return {self.tenant_id, self.owner_id}
