"""Inspection and damage-assessment schemas (SRS O15, O16, T13 steps 7-8)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, field_validator, model_validator

from app.core.clock import ensure_utc
from app.domain.enums import (
    DamageSeverity,
    DeductionCategory,
    DisputeStatus,
    InspectionStatus,
    PropertyCondition,
)
from app.schemas.common import ApiModel, CommandModel, LongText, Money, Reason, ShortText


class SlotProposal(CommandModel):
    starts_at: datetime
    ends_at: datetime
    note: str | None = Field(default=None, max_length=500)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        try:
            return ensure_utc(v)
        except ValueError as exc:
            raise ValueError("must include a UTC offset, e.g. 2026-09-01T09:00:00Z") from exc

    @model_validator(mode="after")
    def _ordered(self) -> SlotProposal:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        if (self.ends_at - self.starts_at).total_seconds() > 12 * 3600:
            raise ValueError("an inspection window must not exceed 12 hours")
        return self


class ProposeSlotsRequest(CommandModel):
    """SRS O15: "agency responds with available dates"."""

    slots: list[SlotProposal] = Field(min_length=1, max_length=10)
    inspector_name: ShortText | None = None
    inspector_licence_no: str | None = Field(default=None, max_length=64)
    note: LongText | None = None

    @model_validator(mode="after")
    def _no_overlaps(self) -> ProposeSlotsRequest:
        ordered = sorted(self.slots, key=lambda s: s.starts_at)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if later.starts_at < earlier.ends_at:
                raise ValueError("proposed slots must not overlap")
        return self


class SelectSlotRequest(CommandModel):
    """SRS T13 step 7 / O15: "owner/tenant select date"."""

    slot_id: uuid.UUID


class RescheduleRequest(CommandModel):
    reason: Reason


class DamageItemInput(CommandModel):
    category: DeductionCategory = DeductionCategory.DAMAGE
    severity: DamageSeverity = DamageSeverity.MINOR
    location: ShortText | None = None
    description: LongText
    estimated_cost: Money
    tenant_liable: bool = Field(
        default=True,
        description="False for fair wear and tear, which is not chargeable to the tenant.",
    )
    photo_document_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)


class SubmitInspectionReportRequest(CommandModel):
    """SRS O16: "inspection agency uploads damage report with photos"."""

    conducted_at: datetime
    overall_condition: PropertyCondition
    report_summary: LongText | None = None
    report_document_id: uuid.UUID | None = Field(
        default=None, description="Id of a previously uploaded INSPECTION_REPORT document"
    )
    inspector_name: ShortText | None = None
    damage_items: list[DamageItemInput] = Field(default_factory=list, max_length=200)

    @field_validator("conducted_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        try:
            return ensure_utc(v)
        except ValueError as exc:
            raise ValueError("conducted_at must include a UTC offset") from exc


class AdjustDamageItemRequest(CommandModel):
    """Owner adjustment during damage review (SRS T13 step 8)."""

    approved_cost: Money | None = None
    tenant_liable: bool | None = None
    note: LongText | None = None

    @model_validator(mode="after")
    def _something_to_do(self) -> AdjustDamageItemRequest:
        if self.approved_cost is None and self.tenant_liable is None:
            raise ValueError("supply approved_cost and/or tenant_liable")
        return self


class RaiseDisputeRequest(CommandModel):
    reason: Reason


class ResolveDisputeRequest(CommandModel):
    uphold: bool = Field(description="True accepts the tenant's objection.")
    approved_cost: Money | None = Field(
        default=None,
        description=(
            "Required when upholding: the revised charge, 0.00 for a full waiver."
        ),
    )
    note: LongText | None = None

    @model_validator(mode="after")
    def _upheld_needs_amount(self) -> ResolveDisputeRequest:
        if self.uphold and self.approved_cost is None:
            raise ValueError(
                "approved_cost is required when upholding a dispute "
                "(use 0.00 to waive the charge entirely)"
            )
        return self


# --------------------------------------------------------------- responses
class SlotResponse(ApiModel):
    id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    is_selected: bool
    note: str | None


class DamageItemResponse(ApiModel):
    id: uuid.UUID
    category: DeductionCategory
    severity: DamageSeverity
    location: str | None
    description: str
    estimated_cost: Money
    approved_cost: Money | None
    chargeable_amount: Money
    tenant_liable: bool
    dispute_status: DisputeStatus
    dispute_reason: str | None
    dispute_resolution_note: str | None
    photo_document_ids: list[uuid.UUID] = Field(default_factory=list)
    adjustment_note: str | None
    created_at: datetime


class InspectionResponse(ApiModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    status: InspectionStatus
    round_number: int
    agency_id: uuid.UUID
    agency_name: str
    agency_email: str
    requested_at: datetime
    agency_notified_at: datetime | None
    slots_proposed_at: datetime | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    conducted_at: datetime | None
    reported_at: datetime | None
    inspector_name: str | None
    overall_condition: PropertyCondition | None
    report_summary: str | None
    report_document_id: uuid.UUID | None
    assessed_total: Money | None
    slots: list[SlotResponse] = Field(default_factory=list)
    damage_items: list[DamageItemResponse] = Field(default_factory=list)
