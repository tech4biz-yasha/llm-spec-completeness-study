"""Audit trail.

SRS A3 requires "complete audit trails with 7-year retention". Every state-changing
operation writes one row here inside the same transaction as the change itself, so the
trail cannot diverge from the data. Rows are append-only: nothing in the module issues
an UPDATE or DELETE against this table, and ``retention_until`` lets an archival job
prove a row is out of its retention period before it is moved to cold storage.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import enum_column
from app.domain.enums import ActorRole


class AuditLogEntry(Base):
    __tablename__ = "exit_workflow_audit_log"

    #: A monotonic bigint rather than a UUID: the audit table is the largest and most
    #: append-heavy in the module, and a sequential key keeps the index dense.
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)

    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    workflow_reference: Mapped[str | None] = mapped_column(String(32), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    actor_role: Mapped[ActorRole] = mapped_column(enum_column(ActorRole), nullable=False)
    #: Set when an ADMIN acts on behalf of a party.
    on_behalf_of: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    from_state: Mapped[str | None] = mapped_column(String(48), nullable=True)
    to_state: Mapped[str | None] = mapped_column(String(48), nullable=True)

    #: Field-level before/after. Values are redacted by the audit service before landing
    #: here -- no document bytes, no payout account numbers.
    changes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: ``occurred_at + retention period``; an archival job may only touch rows past this.
    retention_until: Mapped[date] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_exit_workflow_audit_log_wf", "workflow_id", "occurred_at"),
        Index("ix_exit_workflow_audit_log_actor", "actor_id", "occurred_at"),
        Index("ix_exit_workflow_audit_log_occurred", "occurred_at"),
        Index("ix_exit_workflow_audit_log_retention", "retention_until"),
        Index("ix_exit_workflow_audit_log_request", "request_id"),
        {"comment": "Append-only audit trail, 7-year retention (SRS A3)."},
    )
