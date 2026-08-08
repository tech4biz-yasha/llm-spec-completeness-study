"""The digital Exit NOC (SRS T13 step 10, O16).

The rendered PDF is stored inline. It is a small, immutable, legally meaningful artefact that
must stay consistent with the settlement row it was generated from, so keeping it in the same
transactional store avoids a dual-write. ``content_sha256`` makes any later tampering
detectable; a deployment that prefers object storage can move ``pdf_bytes`` out and keep the
hash here without touching the rest of the module.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.workflow import ExitWorkflow


class ExitNOC(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "exit_nocs"
    __table_args__ = (
        sa.UniqueConstraint("workflow_id", name="uq_exit_nocs_workflow"),
        sa.UniqueConstraint("noc_number", name="uq_exit_nocs_number"),
        sa.CheckConstraint("byte_size > 0", name="ck_exit_nocs_size_positive"),
        sa.CheckConstraint("download_count >= 0", name="ck_exit_nocs_download_count"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("exit_workflows.id", ondelete="CASCADE"), nullable=False
    )
    settlement_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("deposit_settlements.id", ondelete="RESTRICT"), nullable=False
    )
    #: Human-facing certificate number, e.g. ``NOC-2026-000042``.
    noc_number: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )
    pdf_bytes: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    content_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: Immutable snapshot of every figure and party printed on the certificate.
    snapshot: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    download_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    first_downloaded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_downloaded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))

    workflow: Mapped[ExitWorkflow] = relationship(back_populates="noc")
