"""Uploaded artefacts: tenant attachments, damage photos, inspection reports."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from exit_workflow.domain.enums import ActorType, DocumentType
from exit_workflow.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, pg_enum

if TYPE_CHECKING:  # pragma: no cover
    from exit_workflow.models.inspection import DamageLineItem
    from exit_workflow.models.workflow import ExitWorkflow


class Document(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "exit_document"

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exit_workflow.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Set when the document is photographic evidence for a damage line item.
    damage_line_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("damage_line_item.id", ondelete="SET NULL"), index=True
    )

    document_type: Mapped[DocumentType] = mapped_column(
        pg_enum(DocumentType, "document_type"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    #: Integrity check on download and evidence of tamper-freedom for audit.
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    uploaded_by: Mapped[uuid.UUID] = mapped_column(nullable=False)
    uploaded_by_role: Mapped[ActorType] = mapped_column(
        pg_enum(ActorType, "actor_type"), nullable=False
    )
    uploaded_at: Mapped[datetime] = mapped_column(nullable=False)

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="documents")
    damage_line_item: Mapped[DamageLineItem | None] = relationship(back_populates="photos")

    __table_args__ = (
        Index("ix_exit_document_workflow_type", "workflow_id", "document_type"),
        CheckConstraint("size_bytes > 0", name="size_positive"),
        CheckConstraint("char_length(checksum_sha256) = 64", name="checksum_is_sha256"),
    )
