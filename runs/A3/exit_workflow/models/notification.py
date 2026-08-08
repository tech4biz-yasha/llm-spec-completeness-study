"""Outbound notification log (owner alerts, agency requests, NOC ready).

Rows are created inside the business transaction and delivered by the
background worker, so a rolled-back workflow never emails anybody.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from exit_workflow.domain.enums import NotificationChannel, NotificationStatus
from exit_workflow.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum


class NotificationLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notification_log"

    workflow_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    channel: Mapped[NotificationChannel] = mapped_column(
        pg_enum(NotificationChannel, "notification_channel"),
        nullable=False,
        default=NotificationChannel.EMAIL,
    )
    template: Mapped[str] = mapped_column(String(96), nullable=False)
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(default=dict, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
        pg_enum(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column()
    provider_reference: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (Index("ix_notification_log_due", "status", "available_at"),)
