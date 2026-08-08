"""BR-1 contract eligibility schemas."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class EligibilityBlockResponse(BaseModel):
    rule: str
    subject: str
    subject_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_reference: str
    workflow_status: str
    message: str


class ContractEligibilityResponse(BaseModel):
    """``allowed=false`` means the caller must block contract creation and show
    ``warning_messages`` (BR-1)."""

    allowed: bool
    blocks: list[EligibilityBlockResponse]
    warning_messages: list[str]
