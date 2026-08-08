"""Inspection and damage-report schemas (O15 / O16)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from exit_workflow.api.schemas.common import ApiModel, Money, MoneyIn
from exit_workflow.domain.enums import (
    DamageCategory,
    DamageReportStatus,
    DamageSeverity,
    InspectionStatus,
    SlotStatus,
    TenantReviewDecision,
)


class RequestInspectionRequest(BaseModel):
    agency_id: uuid.UUID
    notes: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}


class SlotInputSchema(BaseModel):
    starts_at: AwareDatetime
    ends_at: AwareDatetime
    note: str | None = Field(default=None, max_length=512)

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _ordered(self) -> SlotInputSchema:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class ProposeSlotsRequest(BaseModel):
    slots: list[SlotInputSchema] = Field(min_length=1, max_length=10)

    model_config = {"extra": "forbid"}


class ScheduleInspectionRequest(BaseModel):
    slot_id: uuid.UUID

    model_config = {"extra": "forbid"}


class CancelInspectionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)

    model_config = {"extra": "forbid"}


class DamageLineItemRequest(BaseModel):
    category: DamageCategory
    severity: DamageSeverity
    description: str = Field(min_length=3, max_length=2000)
    assessed_amount: MoneyIn
    location: str | None = Field(default=None, max_length=255)
    tenant_liable: bool = True
    notes: str | None = Field(default=None, max_length=2000)
    photo_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)

    model_config = {"extra": "forbid"}


class SubmitDamageReportRequest(BaseModel):
    """Photos are uploaded first as DAMAGE_PHOTO documents, then referenced."""

    inspected_at: AwareDatetime
    line_items: list[DamageLineItemRequest] = Field(default_factory=list, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)
    inspector_name: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid"}


class TenantReviewRequest(BaseModel):
    decision: TenantReviewDecision
    note: str | None = Field(default=None, max_length=2000)

    model_config = {"extra": "forbid"}


class ResolveDisputeRequest(BaseModel):
    resolution_note: str = Field(min_length=3, max_length=2000)

    model_config = {"extra": "forbid"}


class SlotResponse(ApiModel):
    id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    status: SlotStatus
    note: str | None


class InspectionResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    reference: str
    attempt_no: int
    status: InspectionStatus
    agency_id: uuid.UUID
    agency_name: str
    agency_email: str
    request_notes: str | None
    requested_at: datetime
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    conducted_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancellation_reason: str | None
    slots: list[SlotResponse] = []


class DamagePhotoResponse(ApiModel):
    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int


class DamageLineItemResponse(ApiModel):
    id: uuid.UUID
    category: DamageCategory
    severity: DamageSeverity
    description: str
    location: str | None
    assessed_amount: Money
    tenant_liable: bool
    notes: str | None
    photos: list[DamagePhotoResponse] = []


class DamageReportResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    inspection_id: uuid.UUID
    status: DamageReportStatus
    summary: str | None
    inspector_name: str | None
    inspected_at: datetime
    submitted_at: datetime
    currency: str
    assessed_total: Money
    finalized_total: Money | None
    finalized_at: datetime | None
    adjustment_reason: str | None
    tenant_reviewed_at: datetime | None
    tenant_review_note: str | None
    dispute_reason: str | None
    dispute_resolved_at: datetime | None
    dispute_resolution_note: str | None
    line_items: list[DamageLineItemResponse] = []
