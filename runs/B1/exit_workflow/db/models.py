"""SQLAlchemy 2.x mappings.

AGENTS.md names Motor/MongoDB for the workflow document. The workflow lives in
PostgreSQL here instead, because rules.yaml#EXIT-03 requires the workflow insert,
the ``property.exitLock`` flip and the audit row to happen "IN THE SAME
TRANSACTION", and those last two are PostgreSQL rows. A workflow document in a
second store cannot join that transaction, so the atomicity the rule demands
would be unachievable. This is recorded as blockers.md#B-7; the repository layer
is a seam, so a Mongo-backed document store can replace this one once the
atomicity question is decided.

``properties`` and ``contracts`` are owned by other modules. They are mapped
here read-mostly: this module reads contract status and deposit, and toggles
``properties.exit_lock`` (rules.yaml#EXIT-03).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from exit_workflow.db.base import Base
from exit_workflow.domain.enums import (
    AdminTaskStatus,
    AdminTaskType,
    OutboxStatus,
    PaymentStatus,
    PaymentType,
)
from exit_workflow.domain.ids import SEQUENCE_NAME
from exit_workflow.domain.money import CURRENCY
from exit_workflow.domain.states import State

#: rules.yaml#EXIT-02 — server-assigned NNNNN counter.
workflow_number_seq = Sequence(SEQUENCE_NAME, metadata=Base.metadata, start=1, increment=1)

_STATE_VALUES = ", ".join(f"'{state.value}'" for state in State)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Property(TimestampMixin, Base):
    """Owned by the property module. This module toggles the exit lock only."""

    __tablename__ = "properties"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    #: rules.yaml#EXIT-03 — blocks new contracts (BR-1); released only by COMPLETE.
    exit_lock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Which workflow holds the lock, so edges.yaml#X-006 can name it in the 409.
    exit_lock_workflow_id: Mapped[str | None] = mapped_column(String(20), nullable=True)


class Contract(TimestampMixin, Base):
    """Owned by the contracts module. Read here for status and deposit."""

    __tablename__ = "contracts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    #: AGENTS.md — money in minor units (fils).
    security_deposit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default=CURRENCY)

    __table_args__ = (
        CheckConstraint("security_deposit_minor >= 0", name="ck_contracts_deposit_non_negative"),
        CheckConstraint(f"currency = '{CURRENCY}'", name="ck_contracts_currency_aed"),
    )


class ExitWorkflow(TimestampMixin, Base):
    """The workflow document. rules.yaml#EXIT-01..EXIT-09."""

    __tablename__ = "exit_workflows"

    #: rules.yaml#EXIT-02 — EX-YYYYMMDD-NNNNN, server assigned.
    id: Mapped[str] = mapped_column(String(20), primary_key=True)

    #: rules.yaml#EXIT-01, edges.yaml#X-001 — one workflow per contract. The
    #: uniqueness is enforced here, not in application code, so two concurrent
    #: initiations cannot both pass a read-then-write check.
    contract_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contracts.id"), nullable=False)
    property_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("properties.id"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    status: Mapped[str] = mapped_column(String(32), nullable=False)

    # --- Initiation (rules.yaml#EXIT-02, edges.yaml#X-007) --------------------
    #: Asia/Dubai calendar day, stored as a date and never as a datetime.
    move_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    documents: Mapped[list] = mapped_column(JSONB, nullable=False)

    #: Snapshot of the contract deposit at initiation, so a later contract edit
    #: cannot change the refund arithmetic of an exit already under way.
    security_deposit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # --- Inspection and damage (rules.yaml#EXIT-06) ---------------------------
    inspection_scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    damage_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    damage_photos: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    inspection_reported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    damage_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Settlement (rules.yaml#EXIT-07, EXIT-08) -----------------------------
    refund_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id"), nullable=True, unique=True
    )

    # --- NOC and completion (rules.yaml#EXIT-09) ------------------------------
    noc_document_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, unique=True)
    noc_issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- Stall (rules.yaml#EXIT-05) -------------------------------------------
    stalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("contract_id", name="uq_exit_workflows_contract"),
        CheckConstraint(f"status IN ({_STATE_VALUES})", name="ck_exit_workflows_status"),
        CheckConstraint(
            "jsonb_typeof(documents) = 'array' AND jsonb_array_length(documents) >= 1",
            name="ck_exit_workflows_documents_required",
        ),
        CheckConstraint(
            "damage_amount_minor IS NULL OR damage_amount_minor >= 0",
            name="ck_exit_workflows_damage_non_negative",
        ),
        CheckConstraint(
            "refund_amount_minor IS NULL OR refund_amount_minor >= 0",
            name="ck_exit_workflows_refund_non_negative",
        ),
        CheckConstraint(
            "security_deposit_minor >= 0", name="ck_exit_workflows_deposit_non_negative"
        ),
        Index("ix_exit_workflows_property", "property_id"),
        Index("ix_exit_workflows_tenant", "tenant_id"),
        Index("ix_exit_workflows_status_move_out", "status", "move_out_date"),
    )


class ExitWorkflowAudit(Base):
    """rules.yaml#EXIT-10 — actor, timestamp, from, to, metadata.

    Append-only. AGENTS.md: "Audit rows are append-only. Enforced by DB trigger,
    not application code." The trigger lives in the migration; no ORM mapping
    can weaken it.
    """

    __tablename__ = "exit_workflow_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    from_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    rule_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    audit_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_exit_workflow_audit_workflow", "workflow_id", "occurred_at"),
        CheckConstraint(f"to_state IN ({_STATE_VALUES})", name="ck_audit_to_state"),
        CheckConstraint(
            f"from_state IS NULL OR from_state IN ({_STATE_VALUES})", name="ck_audit_from_state"
        ),
    )


class Payment(TimestampMixin, Base):
    """rules.yaml#EXIT-08 — DEPOSIT_REFUND with idempotency key = workflow ID."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    contract_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    payee_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default=CURRENCY)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=PaymentStatus.PENDING)

    #: edges.yaml#X-005 — unique, so a settlement race creates one payment.
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    gateway_reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gateway_status_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        CheckConstraint("amount_minor >= 0", name="ck_payments_amount_non_negative"),
        CheckConstraint(f"currency = '{CURRENCY}'", name="ck_payments_currency_aed"),
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{status.value}'" for status in PaymentStatus)
            + ")",
            name="ck_payments_status",
        ),
        CheckConstraint(
            "payment_type IN (" + ", ".join(f"'{t.value}'" for t in PaymentType) + ")",
            name="ck_payments_type",
        ),
    )


class NocDocument(Base):
    """rules.yaml#EXIT-09 — PDF in the UAE bucket, immutable once issued.

    Immutability is enforced by a DB trigger alongside the object store's own
    write-once policy: the row records where the bytes are and their digest, so
    an altered object is detectable even if the bucket policy is relaxed later.
    """

    __tablename__ = "noc_documents"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("bucket", "object_key", name="uq_noc_object"),)


class EventOutbox(Base):
    """Transactional outbox for Kafka events.

    rules.yaml#EXIT-04 requires the owner notification to be emitted *after* the
    initiation transaction commits, and requires that a dispatch failure never
    rolls the workflow back. Writing the intent to this table inside the
    transaction and dispatching from it afterwards gives both: the event cannot
    be lost if the process dies between commit and publish, and a publish
    failure cannot reach back into the workflow transaction.
    """

    __tablename__ = "event_outbox"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    topic: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    event_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=OutboxStatus.PENDING)
    #: rules.yaml#EXIT-04 — 5 attempts, exponential backoff, then dead-letter.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s.value}'" for s in OutboxStatus) + ")",
            name="ck_event_outbox_status",
        ),
        Index("ix_event_outbox_dispatchable", "status", "next_attempt_at"),
    )


class AdminTask(Base):
    """Work items for the admin console (rules.yaml#EXIT-04, #EXIT-05)."""

    __tablename__ = "admin_tasks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AdminTaskStatus.OPEN)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "task_type IN (" + ", ".join(f"'{t.value}'" for t in AdminTaskType) + ")",
            name="ck_admin_tasks_type",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s.value}'" for s in AdminTaskStatus) + ")",
            name="ck_admin_tasks_status",
        ),
        Index("ix_admin_tasks_open", "task_type", "status"),
    )
