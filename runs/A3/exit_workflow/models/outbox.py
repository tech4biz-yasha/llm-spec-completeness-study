"""Transactional outbox for Kafka publication (§7).

Domain events are written in the same transaction as the state change they
describe, then relayed asynchronously. This is what makes "exit approved" and
"exit.approved was published" impossible to disagree.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from exit_workflow.domain.enums import OutboxStatus
from exit_workflow.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum


class OutboxEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "outbox_event"

    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    #: Kafka partition key — all events for one workflow stay ordered.
    partition_key: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    status: Mapped[OutboxStatus] = mapped_column(
        pg_enum(OutboxStatus, "outbox_status"), nullable=False, default=OutboxStatus.PENDING
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    published_at: Mapped[datetime | None] = mapped_column()
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # The relay's hot path: pending work that is due, oldest first.
        Index("ix_outbox_event_due", "status", "available_at"),
    )
