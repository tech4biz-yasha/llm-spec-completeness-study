"""Exit NOC schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from exit_workflow.api.schemas.common import ApiModel, Money


class NocResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    noc_number: str
    verification_code: str
    issued_at: datetime
    property_id: uuid.UUID
    property_reference: str | None
    property_address: str | None
    tenant_id: uuid.UUID
    tenant_name: str | None
    owner_name: str | None
    contract_id: uuid.UUID
    move_out_date: date
    currency: str
    security_deposit_amount: Money
    total_deduction_amount: Money
    refund_amount: Money
    content_sha256: str
    size_bytes: int
    download_count: int
    first_downloaded_at: datetime | None
    revoked_at: datetime | None
    revocation_reason: str | None


class NocVerificationResponse(BaseModel):
    """Deliberately minimal: enough to prove a certificate is genuine, no more."""

    noc_number: str
    valid: bool
    issued_at: str
    property_reference: str | None
    move_out_date: str
    revoked: bool
    revocation_reason: str | None


class RevokeNocRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)

    model_config = {"extra": "forbid"}
