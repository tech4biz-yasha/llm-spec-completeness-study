"""Exit workflow request/response schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field, field_validator, model_validator

from app.domain.enums import ActorRole, ExitReason, ExitWorkflowState
from app.schemas.common import ApiModel, CommandModel, LongText, Money, Reason, ShortText


class PartySnapshot(CommandModel):
    """Point-in-time party details captured for documents and notifications."""

    name: ShortText
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    identifier: str | None = Field(
        default=None, max_length=64, description="Emirates ID / trade licence, if known"
    )

    @field_validator("email")
    @classmethod
    def _basic_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("must be a valid email address")
        return v


class PropertySnapshot(CommandModel):
    reference: ShortText = Field(description="Owner-facing property reference / unit number")
    address: ShortText
    community: str | None = Field(default=None, max_length=255)
    emirate: str = Field(default="Dubai", max_length=64)


class InitiateExitRequest(CommandModel):
    """SRS T13 steps 1-3. The move-out date and reason may also be supplied later while
    the workflow is still a draft."""

    property_id: uuid.UUID
    contract_id: uuid.UUID
    tenant_id: uuid.UUID | None = Field(
        default=None,
        description="Defaults to the calling tenant; only ADMIN/OWNER may set it.",
    )
    owner_id: uuid.UUID

    move_out_date: date | None = None
    reason: ExitReason | None = None
    reason_details: Reason | None = None

    security_deposit_amount: Money = Field(
        description="Deposit held under the tenancy contract, in the contract currency."
    )
    currency: str = Field(default="AED", min_length=3, max_length=3)

    property_snapshot: PropertySnapshot
    tenant_snapshot: PartySnapshot
    owner_snapshot: PartySnapshot

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @model_validator(mode="after")
    def _reason_details_required(self) -> InitiateExitRequest:
        if self.reason is ExitReason.OTHER and not self.reason_details:
            raise ValueError("reason_details is required when reason is OTHER")
        return self


class UpdateDraftRequest(CommandModel):
    """Patch a draft (SRS T13 steps 2-3). Only supplied fields are changed."""

    move_out_date: date | None = None
    reason: ExitReason | None = None
    reason_details: Reason | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> UpdateDraftRequest:
        if self.model_fields_set == set():
            raise ValueError("at least one field must be supplied")
        return self


class SubmitExitRequest(CommandModel):
    """SRS T13 step 5-6."""

    acknowledgement: bool = Field(
        default=False,
        description="Tenant confirms the details are correct and the notice is served.",
    )
    note: LongText | None = None

    @field_validator("acknowledgement")
    @classmethod
    def _must_acknowledge(cls, v: bool) -> bool:
        if not v:
            raise ValueError("the exit request must be acknowledged before submission")
        return v


class AgencyAssignment(CommandModel):
    """The registered inspection agency the owner engages (SRS O15)."""

    agency_id: uuid.UUID
    agency_name: ShortText
    agency_email: str = Field(max_length=320)

    @field_validator("agency_email")
    @classmethod
    def _basic_email(cls, v: str) -> str:
        v = v.strip()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError("must be a valid email address")
        return v


class OwnerApproveRequest(CommandModel):
    """SRS O15: owner approves the exit, which requests the inspection."""

    inspection_agency: AgencyAssignment
    note: LongText | None = None
    #: Optional correction to the deposit on record; ADMIN/OWNER only, fully audited.
    security_deposit_amount: Money | None = None


class OwnerRejectRequest(CommandModel):
    reason: Reason


class CancelRequest(CommandModel):
    reason: Reason


class CompleteRequest(CommandModel):
    note: LongText | None = None


# --------------------------------------------------------------- responses
class TimelineEntry(ApiModel):
    from_state: ExitWorkflowState
    to_state: ExitWorkflowState
    action: str
    actor_role: ActorRole
    actor_id: uuid.UUID | None
    note: str | None
    occurred_at: datetime


class ProgressView(ApiModel):
    step: int = Field(description="Position in the SRS T13 ten-step flow")
    total_steps: int = 10
    label: str
    hint: str = ""
    is_terminal: bool
    blocks_new_contracts: bool


class ExitWorkflowSummary(ApiModel):
    id: uuid.UUID
    reference: str | None
    state: ExitWorkflowState
    property_id: uuid.UUID
    contract_id: uuid.UUID
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    move_out_date: date | None
    reason: ExitReason | None
    currency: str
    security_deposit_amount: Money
    net_refund_amount: Money | None
    created_at: datetime
    updated_at: datetime
    submitted_at: datetime | None
    completed_at: datetime | None


class ExitWorkflowDetail(ExitWorkflowSummary):
    reason_details: str | None
    notice_days: int | None
    notice_waived: bool
    total_deductions: Money | None
    tenant_liability_amount: Money | None

    property_snapshot: dict[str, Any]
    tenant_snapshot: dict[str, Any]
    owner_snapshot: dict[str, Any]

    owner_notified_at: datetime | None
    owner_decided_at: datetime | None
    rejection_reason: str | None
    damage_review_opened_at: datetime | None
    dispute_window_closes_at: datetime | None
    noc_issued_at: datetime | None
    closed_at: datetime | None
    closure_reason: str | None

    progress: ProgressView
    available_actions: list[str] = Field(
        default_factory=list, description="Actions the caller may currently perform"
    )
    document_count: int = 0
    has_inspection: bool = False
    has_settlement: bool = False
    has_noc: bool = False
