"""The exit workflow aggregate root, its state history and its supporting documents."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.states import ACTIVE_STATES, ExitWorkflowState, progress_step
from app.models.base import Base, MoneyColumn, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:
    from app.models.catalog import Contract, Owner, Property, Tenant
    from app.models.inspection import InspectionAssignment
    from app.models.noc import ExitNOC
    from app.models.settlement import DepositSettlement

#: SQL literal list of in-flight states, used to keep ``is_active`` honest at the DB level.
_ACTIVE_STATES_SQL = ", ".join(f"'{state.value}'" for state in sorted(ACTIVE_STATES))

#: ``ExitWorkflow`` maps a relationship named ``property``, which shadows the builtin inside
#: that class body. Alias it here so computed attributes can still be declared.
computed = property


class ExitReasonCode(StrEnum):
    LEASE_EXPIRY = "LEASE_EXPIRY"
    EARLY_TERMINATION = "EARLY_TERMINATION"
    RELOCATION = "RELOCATION"
    JOB_CHANGE = "JOB_CHANGE"
    PROPERTY_PURCHASED = "PROPERTY_PURCHASED"
    LANDLORD_REQUEST = "LANDLORD_REQUEST"
    OTHER = "OTHER"


class ActorType(StrEnum):
    TENANT = "TENANT"
    OWNER = "OWNER"
    AGENCY = "AGENCY"
    ADMIN = "ADMIN"
    SYSTEM = "SYSTEM"


class ExitDocumentKind(StrEnum):
    EMIRATES_ID = "EMIRATES_ID"
    PASSPORT = "PASSPORT"
    TENANCY_CONTRACT = "TENANCY_CONTRACT"
    DEWA_FINAL_BILL = "DEWA_FINAL_BILL"
    KEYS_HANDOVER = "KEYS_HANDOVER"
    CLEARANCE_LETTER = "CLEARANCE_LETTER"
    OTHER = "OTHER"


class ExitWorkflow(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One tenant exit, from initiation (T13 step 1) through completion (step 10).

    Concurrency: every state change takes a ``SELECT ... FOR UPDATE`` row lock on this row
    *and* bumps ``version`` (SQLAlchemy optimistic locking), so two racing requests cannot
    both advance the same workflow.
    """

    __tablename__ = "exit_workflows"
    __table_args__ = (
        # BR-1 enforcement, at the storage layer rather than only in application code:
        # at most one in-flight workflow per contract and per property.
        sa.Index(
            "uq_exit_workflows_active_contract",
            "contract_id",
            unique=True,
            postgresql_where=sa.text("is_active"),
        ),
        sa.Index(
            "uq_exit_workflows_active_property",
            "property_id",
            unique=True,
            postgresql_where=sa.text("is_active"),
        ),
        sa.Index("ix_exit_workflows_tenant_active", "tenant_id", "is_active"),
        sa.Index("ix_exit_workflows_owner_state", "owner_id", "state"),
        sa.CheckConstraint(
            f"(state IN ({_ACTIVE_STATES_SQL})) = is_active",
            name="ck_exit_workflows_is_active_matches_state",
        ),
        sa.CheckConstraint(
            "deposit_snapshot_fils >= 0", name="ck_exit_workflows_deposit_non_negative"
        ),
        sa.CheckConstraint(
            "reason_code <> 'OTHER' OR reason_text IS NOT NULL",
            name="ck_exit_workflows_other_reason_needs_text",
        ),
    )

    #: Human-facing workflow ID (T13 step 5), e.g. ``EXW-2026-000001``.
    reference: Mapped[str] = mapped_column(sa.String(32), nullable=False, unique=True)

    contract_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=False
    )
    # Denormalised from the contract so BR-1 lock checks are single-table index lookups
    # (SRS §5.1 requires p95 < 200 ms).
    property_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("owners.id", ondelete="RESTRICT"), nullable=False
    )

    state: Mapped[ExitWorkflowState] = mapped_column(
        pg_enum(ExitWorkflowState, "exit_workflow_state"),
        nullable=False,
        default=ExitWorkflowState.DRAFT,
    )
    #: Mirror of ``state in ACTIVE_STATES``; exists purely to support partial unique indexes.
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    move_out_date: Mapped[date] = mapped_column(sa.Date, nullable=False)  # T13 step 2
    reason_code: Mapped[ExitReasonCode] = mapped_column(  # T13 step 3
        pg_enum(ExitReasonCode, "exit_reason_code"), nullable=False
    )
    reason_text: Mapped[str | None] = mapped_column(sa.Text)

    #: Deposit captured at initiation. Frozen here so a later contract amendment cannot
    #: silently change the basis of an in-flight settlement.
    deposit_snapshot_fils: Mapped[int] = mapped_column(MoneyColumn, nullable=False)

    initiated_by_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False, default=ActorType.TENANT
    )

    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    owner_approved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    inspection_completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    noc_issued_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    closed_reason: Mapped[str | None] = mapped_column(sa.Text)

    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    # Optimistic locking: any UPDATE carries `WHERE version = <loaded value>`. Combined with
    # the FOR UPDATE row lock taken by WorkflowService, a lost update is impossible; a racing
    # writer gets StaleDataError, which the API layer maps to 409.
    __mapper_args__ = {"version_id_col": version}

    contract: Mapped[Contract] = relationship(back_populates="exit_workflows", lazy="selectin")
    property: Mapped[Property] = relationship(lazy="selectin")
    tenant: Mapped[Tenant] = relationship(lazy="selectin")
    owner: Mapped[Owner] = relationship(lazy="selectin")
    documents: Mapped[list[ExitDocument]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", lazy="selectin"
    )
    transitions: Mapped[list[ExitWorkflowTransition]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="ExitWorkflowTransition.occurred_at",
        lazy="selectin",
    )
    assignments: Mapped[list[InspectionAssignment]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="InspectionAssignment.attempt",
        lazy="selectin",
    )
    settlement: Mapped[DepositSettlement | None] = relationship(
        back_populates="workflow", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )
    noc: Mapped[ExitNOC | None] = relationship(
        back_populates="workflow", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )

    @computed
    def progress_step(self) -> int | None:
        return progress_step(self.state)

    @computed
    def current_assignment(self) -> InspectionAssignment | None:
        return self.assignments[-1] if self.assignments else None

    def apply_state(self, new_state: ExitWorkflowState) -> None:
        """Set state and keep the ``is_active`` mirror in sync. Callers must have validated."""
        self.state = new_state
        self.is_active = new_state in ACTIVE_STATES


class ExitWorkflowTransition(UUIDPrimaryKeyMixin, Base):
    """Append-only state history. One row per accepted transition."""

    __tablename__ = "exit_workflow_transitions"
    __table_args__ = (
        sa.Index("ix_exit_workflow_transitions_workflow", "workflow_id", "occurred_at"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exit_workflows.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[ExitWorkflowState] = mapped_column(
        pg_enum(ExitWorkflowState, "exit_workflow_state"), nullable=False
    )
    to_state: Mapped[ExitWorkflowState] = mapped_column(
        pg_enum(ExitWorkflowState, "exit_workflow_state"), nullable=False
    )
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(sa.Text)
    context: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="transitions")


class ExitDocument(UUIDPrimaryKeyMixin, Base):
    """A document uploaded against the exit request (T13 step 4).

    Bytes live in object storage; only the key, size and checksum are held relationally.
    """

    __tablename__ = "exit_documents"
    __table_args__ = (
        sa.Index("ix_exit_documents_workflow_kind", "workflow_id", "kind"),
        sa.CheckConstraint("byte_size > 0", name="ck_exit_documents_size_positive"),
        sa.UniqueConstraint("storage_key", name="uq_exit_documents_storage_key"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exit_workflows.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ExitDocumentKind] = mapped_column(
        pg_enum(ExitDocumentKind, "exit_document_kind"), nullable=False
    )
    file_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(sa.String(120), nullable=False)
    byte_size: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(sa.String(64))
    uploaded_by_type: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    uploaded_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="documents")
