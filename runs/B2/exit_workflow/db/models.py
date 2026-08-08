"""SQLAlchemy 2.x models. These mirror migrations/0001_exit_workflow.sql exactly.

The SQL migration is the schema of record (it carries the append-only triggers,
which cannot be expressed in the ORM). These classes are the typed read/write
surface over it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..domain.states import State


class Base(DeclarativeBase):
    pass


_workflow_state_enum = SAEnum(
    State,
    name="exit_workflow_state",
    native_enum=True,
    create_type=False,
    values_callable=lambda enum: [member.value for member in enum],
)


class PaymentType(str, Enum):
    """rules.yaml#EXIT-08 — the only payment type this module creates."""

    DEPOSIT_REFUND = "DEPOSIT_REFUND"


class PaymentStatus(str, Enum):
    """Gateway outcomes. algorithm.md step 11: only SUCCEEDED may proceed."""

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OutboxStatus(str, Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    DEAD_LETTERED = "DEAD_LETTERED"


# --------------------------------------------------------------------------
# Externally owned tables (property / contract modules). Read-only here except
# properties.exit_lock, which rules.yaml#EXIT-03 puts under this module's control.
# --------------------------------------------------------------------------


class Property(Base):
    __tablename__ = "properties"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # rules.yaml#EXIT-03 — set with the workflow insert, released by COMPLETE.
    exit_lock: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    security_deposit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'AED'"))


#: rules.yaml#EXIT-01 — the only contract status from which an exit may start.
CONTRACT_STATUS_ACTIVE = "ACTIVE"


# --------------------------------------------------------------------------
# Module tables
# --------------------------------------------------------------------------

#: rules.yaml#EXIT-02 — server-assigned id sequence, PostgreSQL owned.
workflow_id_seq = Sequence("exit_workflow_id_seq", metadata=Base.metadata)


class ExitWorkflow(Base):
    __tablename__ = "exit_workflows"
    __table_args__ = (
        CheckConstraint(r"id ~ '^EX-[0-9]{8}-[0-9]{5}$'", name="ck_exit_workflow_id_format"),
        # rules.yaml#EXIT-01, edges.yaml#X-001 — one open workflow per contract.
        Index(
            "uq_exit_workflow_open_per_contract",
            "contract_id",
            unique=True,
            postgresql_where=text("status <> 'COMPLETE'"),
        ),
        Index("ix_exit_workflows_property", "property_id"),
        Index("ix_exit_workflows_tenant", "tenant_id"),
        Index(
            "ix_exit_workflows_stall_scan",
            "status",
            "move_out_date",
            postgresql_where=text("status IN ('OWNER_NOTIFIED', 'INSPECTION_SCHEDULED')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True)
    contract_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    property_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[State] = mapped_column(_workflow_state_enum, nullable=False)

    # edges.yaml#X-007 — Dubai calendar day, date not datetime.
    move_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    documents: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)

    security_deposit_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)

    inspection_scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    inspection_scheduled_for: Mapped[date | None] = mapped_column(Date)

    damage_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    inspection_photos: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    inspection_reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_damage_minor: Mapped[int | None] = mapped_column(BigInteger)
    damage_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispute_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    refund_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    noc_document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    stalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    __mapper_args__ = {"version_id_col": version}


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (UniqueConstraint("idempotency_key", name="payments_idempotency_key_key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("exit_workflows.id"), nullable=False
    )
    type: Mapped[PaymentType] = mapped_column(
        SAEnum(PaymentType, name="payment_type", native_enum=True, create_type=False), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default=text("'AED'"))
    status: Mapped[PaymentStatus] = mapped_column(
        SAEnum(PaymentStatus, name="payment_status", native_enum=True, create_type=False),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    # rules.yaml#EXIT-08 — idempotency key is the workflow id (edges.yaml#X-005).
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    gateway_reference: Mapped[str | None] = mapped_column(Text)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class NocDocument(Base):
    """rules.yaml#EXIT-09 — immutable once issued (enforced by DB trigger)."""

    __tablename__ = "noc_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("exit_workflows.id"), nullable=False, unique=True
    )
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExitWorkflowAudit(Base):
    """rules.yaml#EXIT-10 — append-only, enforced by DB trigger, 7 year retention."""

    __tablename__ = "exit_workflow_audit"
    __table_args__ = (Index("ix_exit_workflow_audit_workflow", "workflow_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    actor_role: Mapped[str] = mapped_column(Text, nullable=False)
    from_state: Mapped[State | None] = mapped_column(_workflow_state_enum)
    to_state: Mapped[State] = mapped_column(_workflow_state_enum, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OutboxEvent(Base):
    """rules.yaml#EXIT-04, edges.yaml#X-002 — transactional outbox for Kafka."""

    __tablename__ = "exit_workflow_events"
    __table_args__ = (
        Index(
            "ix_exit_workflow_events_due",
            "next_attempt_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("exit_workflows.id"), nullable=False
    )
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    partition_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        SAEnum(OutboxStatus, name="outbox_status", native_enum=True, create_type=False),
        nullable=False,
        server_default=text("'PENDING'"),
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminTask(Base):
    """rules.yaml#EXIT-05 (stall) and rules.yaml#EXIT-04 (dead-letter alert)."""

    __tablename__ = "exit_workflow_admin_tasks"
    __table_args__ = (
        Index(
            "uq_admin_task_open",
            "workflow_id",
            "task_type",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workflow_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("exit_workflows.id"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'OPEN'"))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AdminTaskType(str, Enum):
    STALLED_EXIT = "STALLED_EXIT"  # rules.yaml#EXIT-05
    NOTIFICATION_DEAD_LETTER = "NOTIFICATION_DEAD_LETTER"  # rules.yaml#EXIT-04
