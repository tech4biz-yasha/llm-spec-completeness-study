"""The exit workflow aggregate root."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import MONEY, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column
from app.domain.enums import (
    TERMINAL_STATES,
    ActorRole,
    ExitReason,
    ExitWorkflowState,
)

if TYPE_CHECKING:
    from app.models.document import ExitDocument
    from app.models.inspection import Inspection
    from app.models.noc import ExitNoc
    from app.models.settlement import Settlement


_TERMINAL_SQL = ", ".join(f"'{s.value}'" for s in sorted(TERMINAL_STATES))
#: Predicate identifying workflows that hold the BR-1 lock.
ACTIVE_PREDICATE = text(f"state NOT IN ({_TERMINAL_SQL})")


class ExitWorkflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One tenant exit, from initiation (T13) through NOC issuance (O16)."""

    __tablename__ = "exit_workflow"

    # ------------------------------------------------------------- identity
    #: NULL while the workflow is a draft: SRS T13 sequences "Workflow ID generation" at
    #: step 5, after the tenant has supplied the date, reason and documents. Postgres
    #: permits many NULLs under a UNIQUE constraint, so drafts coexist happily.
    reference: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        unique=True,
        comment="Human-facing Workflow ID (T13 step 5), e.g. EXW-2026-000123",
    )

    # ------------------------------------------------------------- parties
    property_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    #: Denormalised for the NOC document and notifications. The authoritative copies live
    #: in the Property/Identity services; these are a point-in-time snapshot so a
    #: seven-year-old NOC still renders exactly as issued.
    property_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    tenant_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    owner_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # --------------------------------------------------------------- state
    state: Mapped[ExitWorkflowState] = mapped_column(
        enum_column(ExitWorkflowState),
        nullable=False,
        default=ExitWorkflowState.DRAFT,
    )
    #: Optimistic concurrency token; SQLAlchemy bumps and checks it on every UPDATE.
    version: Mapped[int] = mapped_column(nullable=False, default=1, server_default=text("1"))

    # ------------------------------------------- T13 steps 2-3: date, reason
    move_out_date: Mapped[date | None] = mapped_column(nullable=True, index=True)
    reason: Mapped[ExitReason | None] = mapped_column(enum_column(ExitReason), nullable=True)
    reason_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    notice_days: Mapped[int | None] = mapped_column(nullable=True)
    notice_waived: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )

    # ------------------------------------------------------------- money
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="AED", server_default=text("'AED'")
    )
    security_deposit_amount: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, server_default=text("0")
    )
    total_deductions: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    net_refund_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    tenant_liability_amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)

    # ------------------------------------------------------- lifecycle marks
    initiated_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    initiated_by_role: Mapped[ActorRole] = mapped_column(
        enum_column(ActorRole), nullable=False, default=ActorRole.TENANT
    )
    submitted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    owner_notified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    owner_decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    owner_decision_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    damage_review_opened_at: Mapped[datetime | None] = mapped_column(nullable=True)
    dispute_window_closes_at: Mapped[datetime | None] = mapped_column(nullable=True)
    noc_issued_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(
        nullable=True, comment="Set for every terminal state, including cancel/reject/expire"
    )
    closed_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    closure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # ------------------------------------------------------- relationships
    documents: Mapped[list[ExitDocument]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ExitDocument.created_at",
    )
    inspection: Mapped[Inspection | None] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    settlement: Mapped[Settlement | None] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    noc: Mapped[ExitNoc | None] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    transitions: Mapped[list[StateTransition]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="noload",
        order_by="StateTransition.occurred_at",
    )

    __mapper_args__ = {"version_id_col": version}

    __table_args__ = (
        # BR-1: at most one *active* exit workflow per property, and per contract.
        # Enforced in the database so a race between two API workers cannot create two.
        Index(
            "uq_exit_workflow_active_property",
            "property_id",
            unique=True,
            postgresql_where=ACTIVE_PREDICATE,
        ),
        Index(
            "uq_exit_workflow_active_contract",
            "contract_id",
            unique=True,
            postgresql_where=ACTIVE_PREDICATE,
        ),
        # Listing endpoints: tenant app and owner portal inboxes.
        Index("ix_exit_workflow_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_exit_workflow_owner_created", "owner_id", "created_at", "id"),
        # BR-1 lookup path.
        Index(
            "ix_exit_workflow_active_tenant",
            "tenant_id",
            postgresql_where=ACTIVE_PREDICATE,
        ),
        # Reconciler scans.
        Index("ix_exit_workflow_state_updated", "state", "updated_at"),
        CheckConstraint(
            "security_deposit_amount >= 0",
            name="security_deposit_non_negative",
        ),
        CheckConstraint(
            "total_deductions IS NULL OR total_deductions >= 0",
            name="total_deductions_non_negative",
        ),
        CheckConstraint(
            "net_refund_amount IS NULL OR net_refund_amount >= 0",
            name="net_refund_non_negative",
        ),
        CheckConstraint(
            "tenant_liability_amount IS NULL OR tenant_liability_amount >= 0",
            name="tenant_liability_non_negative",
        ),
        CheckConstraint("char_length(currency) = 3", name="currency_iso4217"),
        # Once submitted, the date and reason (T13 steps 2-3) are mandatory. Drafts and
        # workflows abandoned while still drafts are exempt.
        CheckConstraint(
            "state IN ('DRAFT', 'CANCELLED', 'EXPIRED') "
            "OR (move_out_date IS NOT NULL AND reason IS NOT NULL AND reference IS NOT NULL)",
            name="submitted_requires_date_reason_reference",
        ),
        {"comment": "Tenant exit workflow aggregate (SRS T13, O15, O16, BR-1)."},
    )

    # ------------------------------------------------------------ helpers
    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_blocking(self) -> bool:
        """True while this workflow holds the BR-1 lock."""
        return not self.is_terminal

    def involves(self, actor_id: uuid.UUID) -> bool:
        return actor_id in (self.tenant_id, self.owner_id)


class StateTransition(UUIDPrimaryKeyMixin, Base):
    """Append-only record of every state change (feeds the client's timeline view)."""

    __tablename__ = "exit_workflow_state_transition"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[ExitWorkflowState] = mapped_column(
        enum_column(ExitWorkflowState, name="from_state"), nullable=False
    )
    to_state: Mapped[ExitWorkflowState] = mapped_column(
        enum_column(ExitWorkflowState, name="to_state"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_role: Mapped[ActorRole] = mapped_column(enum_column(ActorRole), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="transitions")

    __table_args__ = (
        Index("ix_exit_workflow_state_transition_wf", "workflow_id", "occurred_at"),
        {"comment": "Immutable state-change history for the exit workflow."},
    )
