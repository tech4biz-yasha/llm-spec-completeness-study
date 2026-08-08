"""Audit trail and the transactional outbox.

SRS A3 requires complete audit trails with seven-year retention: every audit row carries an
explicit ``retain_until`` date so a retention job can prune by predicate instead of guesswork.

The outbox exists because the SRS puts Kafka on the event path (§7) while PostgreSQL holds the
state. Writing the event row in the *same* transaction as the state change, and publishing
from the outbox afterwards, removes the dual-write failure mode where a workflow advances but
its event is lost (or vice versa).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, pg_enum
from app.models.workflow import ActorType


class AuditAction(StrEnum):
    WORKFLOW_INITIATED = "WORKFLOW_INITIATED"
    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"
    WORKFLOW_SUBMITTED = "WORKFLOW_SUBMITTED"
    OWNER_APPROVED = "OWNER_APPROVED"
    OWNER_REJECTED = "OWNER_REJECTED"
    INSPECTION_REQUESTED = "INSPECTION_REQUESTED"
    INSPECTION_SLOTS_PROPOSED = "INSPECTION_SLOTS_PROPOSED"
    INSPECTION_SCHEDULED = "INSPECTION_SCHEDULED"
    INSPECTION_COMPLETED = "INSPECTION_COMPLETED"
    DAMAGE_REPORT_SUBMITTED = "DAMAGE_REPORT_SUBMITTED"
    SETTLEMENT_COMPUTED = "SETTLEMENT_COMPUTED"
    SETTLEMENT_APPROVED = "SETTLEMENT_APPROVED"
    SETTLEMENT_DISPUTED = "SETTLEMENT_DISPUTED"
    PAYMENT_INITIATED = "PAYMENT_INITIATED"
    PAYMENT_SUCCEEDED = "PAYMENT_SUCCEEDED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    SETTLEMENT_CLOSED = "SETTLEMENT_CLOSED"
    NOC_ISSUED = "NOC_ISSUED"
    NOC_DOWNLOADED = "NOC_DOWNLOADED"
    WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
    WORKFLOW_CANCELLED = "WORKFLOW_CANCELLED"
    CONTRACT_BLOCKED = "CONTRACT_BLOCKED"
    CONTRACT_CREATED = "CONTRACT_CREATED"


class AuditLogEntry(Base):
    """Append-only. Nothing in this module ever updates or deletes an audit row."""

    __tablename__ = "audit_log"
    __table_args__ = (
        sa.Index("ix_audit_log_workflow", "workflow_id", "occurred_at"),
        sa.Index("ix_audit_log_entity", "entity_type", "entity_id"),
        sa.Index("ix_audit_log_occurred_at", "occurred_at"),
        sa.Index("ix_audit_log_retain_until", "retain_until"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    action: Mapped[AuditAction] = mapped_column(pg_enum(AuditAction, "audit_action"), nullable=False)
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    entity_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(sa.UUID(as_uuid=True))
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    ip_address: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.String(512))
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    #: Earliest date this row may be pruned (SRS A3: seven years).
    retain_until: Mapped[date] = mapped_column(sa.Date, nullable=False)


class OutboxEvent(Base):
    """A domain event awaiting publication to Kafka."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        sa.Index(
            "ix_outbox_events_unpublished",
            "created_at",
            postgresql_where=sa.text("published_at IS NULL"),
        ),
        sa.Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
        sa.CheckConstraint("attempts >= 0", name="ck_outbox_attempts_non_negative"),
    )

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(sa.UUID(as_uuid=True), nullable=False)
    #: Partition key; workflows for one property stay ordered.
    partition_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    headers: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(sa.Text)
