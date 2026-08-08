"""Exit workflow request and response models."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.domain.states import TOTAL_STEPS, ExitWorkflowState, allowed_transitions
from app.models.workflow import (
    ActorType,
    ExitDocument,
    ExitDocumentKind,
    ExitReasonCode,
    ExitWorkflow,
    ExitWorkflowTransition,
)
from app.schemas.common import MoneyOut

NonEmptyStr = Annotated[str, Field(min_length=1, max_length=2000)]


# --- requests ---------------------------------------------------------------------------


class InitiateExitRequest(BaseModel):
    """T13 steps 1-3: exit section, move-out date, reason entry."""

    model_config = ConfigDict(extra="forbid")

    contract_id: uuid.UUID
    move_out_date: date
    reason_code: ExitReasonCode
    reason_text: str | None = Field(default=None, max_length=2000)


class DocumentUploadRequest(BaseModel):
    """T13 step 4. Bytes are uploaded directly to object storage; this registers the result."""

    model_config = ConfigDict(extra="forbid")

    kind: ExitDocumentKind
    file_name: Annotated[str, Field(min_length=1, max_length=255)]
    content_type: Annotated[str, Field(min_length=1, max_length=120)]
    byte_size: Annotated[int, Field(gt=0, le=50 * 1024 * 1024)]
    storage_key: Annotated[str, Field(min_length=1, max_length=512)]
    checksum_sha256: Annotated[str | None, Field(default=None, pattern=r"^[0-9a-f]{64}$")] = None


class ApproveExitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Naming an agency here also triggers the O15 inspection request in one step.
    agency_id: uuid.UUID | None = None
    instructions: str | None = Field(default=None, max_length=2000)


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: NonEmptyStr


# --- responses --------------------------------------------------------------------------


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ExitDocumentKind
    file_name: str
    content_type: str
    byte_size: int
    storage_key: str
    checksum_sha256: str | None
    uploaded_by_type: ActorType
    uploaded_at: datetime

    @classmethod
    def of(cls, document: ExitDocument) -> DocumentOut:
        return cls.model_validate(document)


class TransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_state: ExitWorkflowState
    to_state: ExitWorkflowState
    actor_type: ActorType
    actor_id: uuid.UUID | None
    note: str | None
    occurred_at: datetime

    @classmethod
    def of(cls, transition: ExitWorkflowTransition) -> TransitionOut:
        return cls.model_validate(transition)


class ProgressOut(BaseModel):
    """Drives the tenant app's ten-step progress display (T13)."""

    current_step: int | None
    total_steps: int = TOTAL_STEPS
    state: ExitWorkflowState
    is_active: bool
    allowed_next_states: list[ExitWorkflowState]


class ExitWorkflowSummaryOut(BaseModel):
    id: uuid.UUID
    reference: str
    state: ExitWorkflowState
    is_active: bool
    contract_id: uuid.UUID
    property_id: uuid.UUID
    tenant_id: uuid.UUID
    owner_id: uuid.UUID
    move_out_date: date
    reason_code: ExitReasonCode
    progress: ProgressOut
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, workflow: ExitWorkflow) -> ExitWorkflowSummaryOut:
        return cls(
            id=workflow.id,
            reference=workflow.reference,
            state=workflow.state,
            is_active=workflow.is_active,
            contract_id=workflow.contract_id,
            property_id=workflow.property_id,
            tenant_id=workflow.tenant_id,
            owner_id=workflow.owner_id,
            move_out_date=workflow.move_out_date,
            reason_code=workflow.reason_code,
            progress=ProgressOut(
                current_step=workflow.progress_step,
                state=workflow.state,
                is_active=workflow.is_active,
                allowed_next_states=sorted(allowed_transitions(workflow.state)),
            ),
            created_at=workflow.created_at,
            updated_at=workflow.updated_at,
        )


class ExitWorkflowOut(ExitWorkflowSummaryOut):
    reason_text: str | None
    deposit: MoneyOut
    version: int
    submitted_at: datetime | None
    owner_approved_at: datetime | None
    inspection_completed_at: datetime | None
    settled_at: datetime | None
    noc_issued_at: datetime | None
    completed_at: datetime | None
    closed_reason: str | None
    missing_required_documents: list[str] = Field(default_factory=list)
    documents: list[DocumentOut] = Field(default_factory=list)
    transitions: list[TransitionOut] = Field(default_factory=list)

    @classmethod
    def of(  # type: ignore[override]
        cls, workflow: ExitWorkflow, *, missing_documents: list[str] | None = None
    ) -> ExitWorkflowOut:
        summary = ExitWorkflowSummaryOut.of(workflow)
        return cls(
            **summary.model_dump(),
            reason_text=workflow.reason_text,
            deposit=MoneyOut.of(workflow.deposit_snapshot_fils),
            version=workflow.version,
            submitted_at=workflow.submitted_at,
            owner_approved_at=workflow.owner_approved_at,
            inspection_completed_at=workflow.inspection_completed_at,
            settled_at=workflow.settled_at,
            noc_issued_at=workflow.noc_issued_at,
            completed_at=workflow.completed_at,
            closed_reason=workflow.closed_reason,
            missing_required_documents=missing_documents or [],
            documents=[DocumentOut.of(d) for d in workflow.documents],
            transitions=[TransitionOut.of(t) for t in workflow.transitions],
        )


class WorkflowListOut(BaseModel):
    items: list[ExitWorkflowSummaryOut]
    count: int
