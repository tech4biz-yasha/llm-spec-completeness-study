"""Inspection and damage-report request/response models (O15, O16)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.inspection import (
    AssignmentStatus,
    DamageLineItem,
    DamageReport,
    DamageSeverity,
    InspectionAssignment,
    InspectionSlot,
)
from app.schemas.common import MoneyOut


# --- requests ---------------------------------------------------------------------------


class RequestInspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agency_id: uuid.UUID
    instructions: str | None = Field(default=None, max_length=2000)


class SlotProposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> SlotProposalIn:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class ProposeSlotsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slots: Annotated[list[SlotProposalIn], Field(min_length=1, max_length=10)]


class SelectSlotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_id: uuid.UUID


class PhotoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_key: Annotated[str, Field(min_length=1, max_length=512)]
    caption: str | None = Field(default=None, max_length=300)


class DamageLineItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, Field(min_length=1, max_length=64)]
    description: Annotated[str, Field(min_length=1, max_length=2000)]
    severity: DamageSeverity
    amount_fils: Annotated[int, Field(ge=0)]
    location: str | None = Field(default=None, max_length=120)
    photos: list[PhotoIn] = Field(default_factory=list)


class DamageReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, Field(min_length=1, max_length=5000)]
    inspected_at: datetime
    inspector_name: str | None = Field(default=None, max_length=200)
    line_items: Annotated[list[DamageLineItemIn], Field(max_length=200)] = Field(
        default_factory=list
    )
    photos: list[PhotoIn] = Field(default_factory=list)


class ReinspectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agency_id: uuid.UUID
    reason: Annotated[str, Field(min_length=1, max_length=2000)]


# --- responses --------------------------------------------------------------------------


class SlotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    is_selected: bool
    proposed_at: datetime
    selected_at: datetime | None

    @classmethod
    def of(cls, slot: InspectionSlot) -> SlotOut:
        return cls.model_validate(slot)


class DamageLineItemOut(BaseModel):
    id: uuid.UUID
    code: str
    description: str
    location: str | None
    severity: DamageSeverity
    amount: MoneyOut
    photos: list[dict[str, Any]]

    @classmethod
    def of(cls, item: DamageLineItem) -> DamageLineItemOut:
        return cls(
            id=item.id,
            code=item.code,
            description=item.description,
            location=item.location,
            severity=item.severity,
            amount=MoneyOut.of(item.amount_fils),
            photos=item.photos,
        )


class DamageReportOut(BaseModel):
    id: uuid.UUID
    assignment_id: uuid.UUID
    workflow_id: uuid.UUID
    agency_id: uuid.UUID
    summary: str
    inspector_name: str | None
    inspected_at: datetime
    submitted_at: datetime
    total_deductions: MoneyOut
    photos: list[dict[str, Any]]
    line_items: list[DamageLineItemOut]

    @classmethod
    def of(cls, report: DamageReport) -> DamageReportOut:
        return cls(
            id=report.id,
            assignment_id=report.assignment_id,
            workflow_id=report.workflow_id,
            agency_id=report.agency_id,
            summary=report.summary,
            inspector_name=report.inspector_name,
            inspected_at=report.inspected_at,
            submitted_at=report.submitted_at,
            total_deductions=MoneyOut.of(report.total_deductions_fils),
            photos=report.photos,
            line_items=[DamageLineItemOut.of(i) for i in report.line_items],
        )


class AssignmentOut(BaseModel):
    id: uuid.UUID
    workflow_id: uuid.UUID
    agency_id: uuid.UUID
    attempt: int
    status: AssignmentStatus
    requested_at: datetime
    notified_at: datetime | None
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    completed_at: datetime | None
    instructions: str | None
    slots: list[SlotOut]
    has_report: bool

    @classmethod
    def of(cls, assignment: InspectionAssignment) -> AssignmentOut:
        return cls(
            id=assignment.id,
            workflow_id=assignment.workflow_id,
            agency_id=assignment.agency_id,
            attempt=assignment.attempt,
            status=assignment.status,
            requested_at=assignment.requested_at,
            notified_at=assignment.notified_at,
            scheduled_start=assignment.scheduled_start,
            scheduled_end=assignment.scheduled_end,
            completed_at=assignment.completed_at,
            instructions=assignment.instructions,
            slots=[SlotOut.of(s) for s in assignment.slots],
            has_report=assignment.report is not None,
        )


class AssignmentListOut(BaseModel):
    items: list[AssignmentOut]
    count: int
