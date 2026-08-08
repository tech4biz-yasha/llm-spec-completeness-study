"""Document schemas (SRS T13 step 4)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from app.domain.enums import ActorRole, DocumentType, ScanStatus
from app.schemas.common import ApiModel, CommandModel


class DocumentResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    document_type: DocumentType
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    caption: str | None
    uploaded_by: uuid.UUID
    uploaded_by_role: ActorRole
    scan_status: ScanStatus
    created_at: datetime
    download_url: str | None = Field(
        default=None, description="Relative API path from which the bytes can be fetched"
    )


class DocumentListResponse(ApiModel):
    items: list[DocumentResponse]
    total: int


class DeleteDocumentRequest(CommandModel):
    reason: str | None = Field(default=None, max_length=500)
