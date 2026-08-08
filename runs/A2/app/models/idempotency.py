"""Idempotency records for unsafe endpoints.

The money-moving call in this module ("Pay Deposit") must never execute twice because a
client retried on a timeout. Callers pass ``Idempotency-Key``; the first request for a
key reserves a row, and replays return the stored response verbatim.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPrimaryKeyMixin


class IdempotencyRecord(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "exit_workflow_idempotency"

    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Scoping by endpoint prevents a key minted for one operation from short-circuiting
    #: a different one.
    endpoint: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    #: SHA-256 of the canonicalised request body; a mismatch on replay is a client bug
    #: and is reported as 409 rather than silently returning the earlier response.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: NULL while the original request is still in flight -- a concurrent replay sees the
    #: reserved row and is told to retry rather than executing in parallel.
    response_status: Mapped[int | None] = mapped_column(nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("idempotency_key", "endpoint", name="uq_key_endpoint"),
        Index("ix_exit_workflow_idempotency_expires", "expires_at"),
        {"comment": "Idempotency-Key replay protection for unsafe endpoints."},
    )
