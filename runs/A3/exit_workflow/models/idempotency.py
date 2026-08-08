"""Idempotency records for money-moving and side-effecting POSTs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, PrimaryKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from exit_workflow.models.base import Base


class IdempotencyRecord(Base):
    """One row per (scope, key).

    Reserved at the start of the request and completed with the response body
    before commit, so a duplicate submission either replays the stored response
    or is rejected while the original is still in flight.
    """

    __tablename__ = "idempotency_record"

    scope: Mapped[str] = mapped_column(String(96), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: SHA-256 of the canonical request body — reusing a key with a different
    #: payload is a client bug and must not silently replay.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_id: Mapped[uuid.UUID | None] = mapped_column()
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (PrimaryKeyConstraint("scope", "key", name="pk_idempotency_record"),)
