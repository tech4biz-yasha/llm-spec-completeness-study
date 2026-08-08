"""Audit trail (A3: complete trails, 7-year retention).

Two complementary records:

* :class:`WorkflowTransition` — the state machine's own history, small and
  cheap to render as a tenant-facing timeline.
* :class:`AuditEvent` — the compliance log: who did what, from where, with a
  before/after diff. The migration installs a trigger that rejects UPDATE and
  DELETE on this table, so it is append-only at the database level.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from exit_workflow.domain.enums import ActorType, ExitWorkflowStatus
from exit_workflow.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:  # pragma: no cover
    from exit_workflow.models.workflow import ExitWorkflow


class WorkflowTransition(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "workflow_transition"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[ExitWorkflowStatus | None] = mapped_column(
        pg_enum(ExitWorkflowStatus, "exit_workflow_status")
    )
    to_status: Mapped[ExitWorkflowStatus] = mapped_column(
        pg_enum(ExitWorkflowStatus, "exit_workflow_status"), nullable=False
    )
    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="transitions")

    __table_args__ = (
        Index("ix_workflow_transition_workflow_occurred", "workflow_id", "occurred_at"),
    )


class AuditEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_event"

    occurred_at: Mapped[datetime] = mapped_column(nullable=False, index=True)
    #: e.g. ``exit_workflow.submitted``, ``settlement.paid``.
    action: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(index=True)

    actor_type: Mapped[ActorType] = mapped_column(pg_enum(ActorType, "actor_type"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column()
    actor_email: Mapped[str | None] = mapped_column(String(320))

    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))

    changes: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)
    #: Denormalised so a retention job can delete by index without arithmetic.
    retention_until: Mapped[date] = mapped_column(nullable=False, index=True)

    __table_args__ = (
        Index("ix_audit_event_workflow_occurred", "workflow_id", "occurred_at"),
        Index("ix_audit_event_entity", "entity_type", "entity_id"),
    )
