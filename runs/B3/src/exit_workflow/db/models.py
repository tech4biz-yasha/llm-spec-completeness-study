"""ORM models.

Money columns are ``BigInteger`` counts of fils (AGENTS.md: "minor units in storage").
Timestamp columns are ``timestamptz`` and always written UTC. ``move_out_date`` is a
``DATE`` — a calendar day in Asia/Dubai, never a datetime (edges.yaml#X-007).

``Property`` and ``Contract`` are the two rows this module reads and writes across the
module boundary. They are mapped narrowly: only the columns the exit workflow needs, so
the owning modules stay free to extend their own tables.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..enums import (
    AdminTaskStatus,
    AdminTaskType,
    OutboxStatus,
    PaymentStatus,
    PaymentType,
    WorkflowState,
)
from ..money import CURRENCY
from .base import TZ_TIMESTAMP, Base


class Property(Base):
    """The property under the contract. Only the exit-lock columns are owned here.

    rules.yaml#EXIT-03: ``exit_lock`` is set true in the same transaction as the workflow
    insert, blocks new contracts (BR-1), and is released only by workflow COMPLETE.
    """

    __tablename__ = "properties"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    exit_lock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exit_lock_workflow_id: Mapped[str | None] = mapped_column(String(32))
    exit_lock_set_at: Mapped[datetime | None] = mapped_column(TZ_TIMESTAMP)

    __table_args__ = (
        CheckConstraint(
            "(exit_lock IS FALSE AND exit_lock_workflow_id IS NULL) "
            "OR (exit_lock IS TRUE AND exit_lock_workflow_id IS NOT NULL)",
            name="exit_lock_has_workflow",
        ),
    )


class Contract(Base):
    """The tenancy contract being exited. algorithm.md#1 reads ``status``."""

    __tablename__ = "contracts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    security_deposit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default=CURRENCY)

    property: Mapped[Property] = relationship(lazy="select")

    __table_args__ = (
        CheckConstraint("security_deposit_minor >= 0", name="deposit_non_negative"),
        CheckConstraint(f"currency = '{CURRENCY}'", name="currency_is_aed"),
    )


class ExitWorkflow(Base):
    """states.yaml#exit_workflow.

    ``contract_id`` is UNIQUE: rules.yaml#EXIT-01 allows one workflow per contract at any
    time and edges.yaml#X-001 says "Never a second workflow" — the constraint, not the
    application read, is what makes a concurrent double initiation impossible.
    """

    __tablename__ = "exit_workflows"

    #: rules.yaml#EXIT-02 — EX-YYYYMMDD-NNNNN, server assigned, sequence from PostgreSQL.
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    property_id: Mapped[str] = mapped_column(ForeignKey("properties.id"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False)

    # rules.yaml#EXIT-02 / edges.yaml#X-007
    move_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    documents: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)

    #: Snapshot of the deposit at initiation, in fils. The refund is computed from the
    #: contract at settlement time; this column exists for audit reconstruction.
    security_deposit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # rules.yaml#EXIT-06 — entered by the agency, then confirmed by the owner.
    damage_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    damage_photos: Mapped[list[Any] | None] = mapped_column(JSONB)
    inspection_reported_at: Mapped[datetime | None] = mapped_column(TZ_TIMESTAMP)
    damage_confirmed_at: Mapped[datetime | None] = mapped_column(TZ_TIMESTAMP)

    # rules.yaml#EXIT-07 / #EXIT-08
    refund_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    payment_id: Mapped[str | None] = mapped_column(ForeignKey("payments.id"))

    # rules.yaml#EXIT-09
    noc_document_id: Mapped[str | None] = mapped_column(String(64))

    # rules.yaml#EXIT-05
    stalled_at: Mapped[datetime | None] = mapped_column(TZ_TIMESTAMP)

    created_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    contract: Mapped[Contract] = relationship(lazy="select")

    __table_args__ = (
        UniqueConstraint("contract_id", name="uq_exit_workflows_contract_id"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s.value}'" for s in WorkflowState) + ")",
            name="status_in_states_yaml",
        ),
        CheckConstraint(
            "damage_amount_minor IS NULL OR damage_amount_minor >= 0",
            name="damage_non_negative",
        ),
        CheckConstraint(
            "refund_amount_minor IS NULL OR refund_amount_minor >= 0",
            name="refund_non_negative",
        ),
        Index("ix_exit_workflows_status_move_out_date", "status", "move_out_date"),
        Index("ix_exit_workflows_property_id", "property_id"),
    )

    @property
    def state(self) -> WorkflowState:
        return WorkflowState(self.status)


class ExitWorkflowAudit(Base):
    """rules.yaml#EXIT-10 — actor, timestamp, from, to, metadata. Append-only.

    Append-only is enforced by a database trigger, not by application code
    (AGENTS.md, Conventions); see migrations/versions/0001_exit_workflow.py.
    """

    __tablename__ = "exit_workflow_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("exit_workflows.id"), nullable=False, index=True
    )
    actor_id: Mapped[str | None] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    rule_id: Mapped[str | None] = mapped_column(String(16))
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_exit_workflow_audit_created_at", "created_at"),)


class Payment(Base):
    """rules.yaml#EXIT-08 — DEPOSIT_REFUND, idempotency key = workflow ID.

    The UNIQUE on ``idempotency_key`` is what makes edges.yaml#X-005 hold under a race:
    two concurrent settlements cannot produce two payments.
    """

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PaymentType.DEPOSIT_REFUND
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(32), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default=CURRENCY)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PaymentStatus.PENDING)
    gateway_reference: Mapped[str | None] = mapped_column(String(128))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        CheckConstraint("amount_minor >= 0", name="amount_non_negative"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s.value}'" for s in PaymentStatus) + ")",
            name="status_known",
        ),
        CheckConstraint(f"currency = '{CURRENCY}'", name="currency_is_aed"),
    )


class NocDocument(Base):
    """rules.yaml#EXIT-09 — PDF in the UAE bucket, immutable once issued.

    Immutability is enforced by a database trigger alongside the audit table.
    """

    __tablename__ = "noc_documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("exit_workflows.id"), nullable=False, unique=True
    )
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False, default="application/pdf")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=func.now()
    )


class OutboxEvent(Base):
    """rules.yaml#EXIT-04 / edges.yaml#X-002.

    The row is written inside the initiation transaction; dispatch happens after commit.
    Dispatch failure never rolls the workflow back — it only leaves this row PENDING for
    the retry sweep, and after ``notification_max_attempts`` it becomes DEAD_LETTER and
    raises an admin task.
    """

    __tablename__ = "event_outbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OutboxStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    workflow_id: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(TZ_TIMESTAMP)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s.value}'" for s in OutboxStatus) + ")",
            name="status_known",
        ),
        Index("ix_event_outbox_due", "status", "next_attempt_at"),
    )


class AdminTask(Base):
    """rules.yaml#EXIT-05 ("an admin task is created") and #EXIT-04 ("admin alert")."""

    __tablename__ = "admin_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AdminTaskStatus.OPEN)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TZ_TIMESTAMP, nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "type IN (" + ", ".join(f"'{t.value}'" for t in AdminTaskType) + ")",
            name="type_known",
        ),
        # One open task per workflow per type; the stall sweep is idempotent.
        UniqueConstraint("workflow_id", "type", name="uq_admin_tasks_workflow_id"),
    )
