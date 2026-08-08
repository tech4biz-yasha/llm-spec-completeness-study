"""Document metadata.

SRS §7 puts documents in MongoDB. This module keeps only *metadata* in PostgreSQL --
the bytes live behind :class:`app.ports.storage.DocumentStorage` (object storage) and
are addressed by ``storage_key``. That keeps referential integrity and the audit trail
in one transactional store while leaving the blob backend swappable.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import enum_column
from app.domain.enums import ActorRole, DocumentType, ScanStatus

if TYPE_CHECKING:
    from app.models.exit_workflow import ExitWorkflow


class ExitDocument(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "exit_workflow_document"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False
    )
    document_type: Mapped[DocumentType] = mapped_column(
        enum_column(DocumentType), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    uploaded_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    uploaded_by_role: Mapped[ActorRole] = mapped_column(enum_column(ActorRole), nullable=False)

    scan_status: Mapped[ScanStatus] = mapped_column(
        enum_column(ScanStatus), nullable=False, default=ScanStatus.PENDING
    )
    scanned_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: Soft delete: a tenant may replace a document before submission, but the SRS's
    #: 7-year audit requirement means we never hard-delete evidence.
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    deleted_by: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="documents")

    __table_args__ = (
        Index(
            "ix_exit_workflow_document_active",
            "workflow_id",
            "document_type",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("char_length(checksum_sha256) = 64", name="checksum_is_sha256_hex"),
        {"comment": "Metadata for documents attached to an exit workflow."},
    )

    @property
    def is_active(self) -> bool:
        return self.deleted_at is None

    @property
    def is_downloadable(self) -> bool:
        return self.is_active and self.scan_status in (ScanStatus.CLEAN, ScanStatus.SKIPPED)
