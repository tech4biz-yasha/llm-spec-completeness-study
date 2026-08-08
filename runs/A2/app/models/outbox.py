"""Transactional outbox for Kafka publication (SRS §7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, Identity, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.types import enum_column
from app.domain.enums import OutboxStatus


class OutboxMessage(Base):
    """A domain event awaiting relay to Kafka.

    Written in the same transaction as the state change it describes. A dispatcher
    claims batches with ``FOR UPDATE SKIP LOCKED`` so several replicas can drain the
    table concurrently without publishing anything twice. Delivery is at-least-once;
    ``event_id`` is the consumer's deduplication key.
    """

    __tablename__ = "exit_workflow_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True, default=uuid.uuid4)

    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    partition_key: Mapped[str] = mapped_column(String(128), nullable=False)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    status: Mapped[OutboxStatus] = mapped_column(
        enum_column(OutboxStatus), nullable=False, default=OutboxStatus.PENDING
    )
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    #: Earliest time a dispatcher may pick this row up (drives exponential backoff).
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # The dispatcher's only query path; partial so the index stays small once the
        # bulk of the table is PUBLISHED.
        Index(
            "ix_exit_workflow_outbox_claimable",
            "available_at",
            "id",
            postgresql_where=text("status IN ('PENDING', 'FAILED')"),
        ),
        Index("ix_exit_workflow_outbox_aggregate", "aggregate_id", "occurred_at"),
        Index(
            "ix_exit_workflow_outbox_published",
            "published_at",
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        {"comment": "Transactional outbox relaying exit workflow events to Kafka."},
    )
