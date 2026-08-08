"""Exit workflow request/response schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from exit_workflow.api.schemas.common import ApiModel, Money, StepView
from exit_workflow.domain.enums import (
    ActorType,
    DocumentType,
    ExitReason,
    ExitWorkflowStatus,
)


class InitiateExitRequest(BaseModel):
    """T13 steps 1-2. Parties and deposit come from the contract, not here."""

    contract_id: uuid.UUID
    move_out_date: date
    reason: ExitReason
    reason_details: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}

    @field_validator("reason_details")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return v.strip() if v and v.strip() else None


class OwnerDecisionRequest(BaseModel):
    decision: Literal["APPROVE", "REJECT"]
    reason: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}


class CancelWorkflowRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)

    model_config = {"extra": "forbid"}


class DocumentResponse(ApiModel):
    id: uuid.UUID
    document_type: DocumentType
    filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    uploaded_by: uuid.UUID
    uploaded_by_role: ActorType
    uploaded_at: datetime
    damage_line_item_id: uuid.UUID | None = None


class TransitionResponse(ApiModel):
    from_status: ExitWorkflowStatus | None
    to_status: ExitWorkflowStatus
    actor_type: ActorType
    actor_id: uuid.UUID | None
    reason: str | None
    occurred_at: datetime


class ExitWorkflowSummary(ApiModel):
    id: uuid.UUID
    reference: str
    status: ExitWorkflowStatus
    contract_id: uuid.UUID
    property_id: uuid.UUID
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    property_reference: str | None
    move_out_date: date
    reason: ExitReason
    currency: str
    security_deposit_amount: Money
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ExitWorkflowDetail(ExitWorkflowSummary):
    reason_details: str | None
    property_address: str | None
    tenant_name: str | None
    owner_name: str | None
    documents_uploaded_count: int
    initiated_at: datetime
    submitted_at: datetime | None
    owner_decision_at: datetime | None
    owner_rejection_reason: str | None
    inspection_requested_at: datetime | None
    inspection_scheduled_at: datetime | None
    inspection_completed_at: datetime | None
    damage_review_completed_at: datetime | None
    settlement_completed_at: datetime | None
    noc_issued_at: datetime | None
    noc_first_downloaded_at: datetime | None
    closed_at: datetime | None
    closure_reason: str | None
    steps: list[StepView]
    completed_step_count: int
    current_step: StepView | None
    allowed_transitions: list[ExitWorkflowStatus]
