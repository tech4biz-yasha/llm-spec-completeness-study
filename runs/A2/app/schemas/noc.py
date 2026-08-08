"""NOC schemas (SRS T13 step 10)."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import Field

from app.domain.enums import NocStatus
from app.schemas.common import ApiModel, CommandModel, Reason


class NocResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    noc_number: str
    status: NocStatus
    issued_at: datetime
    effective_date: date
    verification_code: str
    verification_url: str
    checksum_sha256: str
    size_bytes: int
    content_type: str
    download_count: int
    last_downloaded_at: datetime | None
    revoked_at: datetime | None
    revocation_reason: str | None
    download_url: str | None = Field(
        default=None, description="Relative API path that streams the PDF"
    )


class NocVerificationResponse(ApiModel):
    """Public, unauthenticated response.

    Deliberately minimal: it confirms that a certificate exists and is valid without
    disclosing the parties, the address or any settlement figure to an anonymous caller.
    """

    valid: bool
    noc_number: str | None = None
    issued_at: datetime | None = None
    effective_date: date | None = None
    status: NocStatus | None = None
    property_reference: str | None = None
    revoked_at: datetime | None = None
    message: str


class RevokeNocRequest(CommandModel):
    reason: Reason
