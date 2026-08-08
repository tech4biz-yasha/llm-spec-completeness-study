"""Digital Exit NOC (SRS T13 step 10 / O16: "auto-generated upon payment")."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column
from app.domain.enums import NocStatus

if TYPE_CHECKING:
    from app.models.exit_workflow import ExitWorkflow


class ExitNoc(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A No Objection Certificate issued at the end of an exit workflow.

    The rendered PDF lives in object storage. We keep its SHA-256 so a downloaded copy
    can be proven unaltered, and a short ``verification_code`` that a third party (a new
    landlord, a utility provider) can check without an account.
    """

    __tablename__ = "exit_workflow_noc"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    noc_number: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    status: Mapped[NocStatus] = mapped_column(
        enum_column(NocStatus), nullable=False, default=NocStatus.ISSUED
    )

    issued_at: Mapped[datetime] = mapped_column(nullable=False)
    issued_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    effective_date: Mapped[date] = mapped_column(nullable=False)

    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="application/pdf",
        server_default=text("'application/pdf'"),
    )
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    verification_code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    verification_url: Mapped[str] = mapped_column(String(512), nullable=False)

    #: Everything printed on the certificate, frozen at issuance.
    rendered_facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    download_count: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    first_downloaded_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(nullable=True)

    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    revocation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="noc")

    __table_args__ = (
        Index("ix_exit_workflow_noc_issued_at", "issued_at"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("download_count >= 0", name="download_count_non_negative"),
        CheckConstraint("char_length(checksum_sha256) = 64", name="checksum_is_sha256_hex"),
        CheckConstraint(
            "(status = 'REVOKED') = (revoked_at IS NOT NULL)", name="revoked_consistency"
        ),
        {"comment": "Digital Exit NOC issued on completion of deposit settlement."},
    )

    @property
    def is_valid(self) -> bool:
        return self.status is NocStatus.ISSUED and self.revoked_at is None
