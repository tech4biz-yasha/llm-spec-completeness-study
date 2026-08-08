"""O15 — inspection request, scheduling and damage report."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, status

from exit_workflow.api.deps import InspectionServiceDep, WorkflowServiceDep
from exit_workflow.api.schemas.inspection import (
    CancelInspectionRequest,
    DamageReportResponse,
    InspectionResponse,
    ProposeSlotsRequest,
    RequestInspectionRequest,
    ResolveDisputeRequest,
    ScheduleInspectionRequest,
    SubmitDamageReportRequest,
    TenantReviewRequest,
)
from exit_workflow.api.v1.common import WORKFLOW_PATH_DESCRIPTION, workflow_identifier
from exit_workflow.services.inspection import DamageReportInput, LineItemInput, SlotInput

router = APIRouter(tags=["inspections"])

WorkflowRef = Annotated[str, Path(description=WORKFLOW_PATH_DESCRIPTION)]


@router.post(
    "/exit-workflows/{workflow_ref}/inspections",
    response_model=InspectionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request an inspection from a registered agency (O15)",
    description=(
        "Owner-only. Emails the agency with the property details and the Workflow ID, "
        "and moves the exit to INSPECTION_REQUESTED."
    ),
)
async def request_inspection(
    workflow_ref: WorkflowRef,
    payload: RequestInspectionRequest,
    workflows: WorkflowServiceDep,
    inspections: InspectionServiceDep,
) -> InspectionResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    inspection = await inspections.request_inspection(
        workflow, agency_id=payload.agency_id, notes=payload.notes
    )
    return InspectionResponse.model_validate(inspection)


@router.get(
    "/exit-workflows/{workflow_ref}/inspections",
    response_model=list[InspectionResponse],
    summary="List inspections for an exit workflow",
)
async def list_inspections(
    workflow_ref: WorkflowRef, workflows: WorkflowServiceDep
) -> list[InspectionResponse]:
    workflow = await workflows.get(workflow_identifier(workflow_ref))
    return [InspectionResponse.model_validate(i) for i in workflow.inspections]


@router.get(
    "/inspections/{inspection_id}",
    response_model=InspectionResponse,
    summary="Fetch one inspection",
)
async def get_inspection(
    inspection_id: uuid.UUID, inspections: InspectionServiceDep
) -> InspectionResponse:
    inspection, _ = await inspections.load(inspection_id)
    return InspectionResponse.model_validate(inspection)


@router.post(
    "/inspections/{inspection_id}/slots",
    response_model=InspectionResponse,
    summary="Agency proposes available dates",
)
async def propose_slots(
    inspection_id: uuid.UUID,
    payload: ProposeSlotsRequest,
    inspections: InspectionServiceDep,
) -> InspectionResponse:
    inspection, workflow = await inspections.load(inspection_id, for_update=True)
    await inspections.propose_slots(
        inspection,
        workflow,
        [SlotInput(starts_at=s.starts_at, ends_at=s.ends_at, note=s.note) for s in payload.slots],
    )
    return InspectionResponse.model_validate(inspection)


@router.post(
    "/inspections/{inspection_id}/schedule",
    response_model=InspectionResponse,
    summary="Owner or tenant selects an inspection date",
)
async def schedule_inspection(
    inspection_id: uuid.UUID,
    payload: ScheduleInspectionRequest,
    inspections: InspectionServiceDep,
) -> InspectionResponse:
    inspection, workflow = await inspections.load(inspection_id, for_update=True)
    await inspections.schedule(inspection, workflow, slot_id=payload.slot_id)
    return InspectionResponse.model_validate(inspection)


@router.post(
    "/inspections/{inspection_id}/cancel",
    response_model=InspectionResponse,
    summary="Cancel an inspection",
)
async def cancel_inspection(
    inspection_id: uuid.UUID,
    payload: CancelInspectionRequest,
    inspections: InspectionServiceDep,
) -> InspectionResponse:
    inspection, workflow = await inspections.load(inspection_id, for_update=True)
    await inspections.cancel(inspection, workflow, reason=payload.reason)
    return InspectionResponse.model_validate(inspection)


@router.post(
    "/inspections/{inspection_id}/damage-report",
    response_model=DamageReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Agency uploads the damage report (O16 input)",
    description=(
        "Photos must already be uploaded as DAMAGE_PHOTO documents on the workflow; "
        "reference their ids from the line items. Completes the inspection and moves "
        "the exit into damage review."
    ),
)
async def submit_damage_report(
    inspection_id: uuid.UUID,
    payload: SubmitDamageReportRequest,
    inspections: InspectionServiceDep,
) -> DamageReportResponse:
    inspection, workflow = await inspections.load(inspection_id, for_update=True)
    report = await inspections.submit_damage_report(
        inspection,
        workflow,
        DamageReportInput(
            inspected_at=payload.inspected_at,
            summary=payload.summary,
            inspector_name=payload.inspector_name,
            line_items=[
                LineItemInput(
                    category=item.category,
                    severity=item.severity,
                    description=item.description,
                    assessed_amount=item.assessed_amount,
                    location=item.location,
                    tenant_liable=item.tenant_liable,
                    notes=item.notes,
                    photo_document_ids=item.photo_document_ids,
                )
                for item in payload.line_items
            ],
        ),
    )
    return DamageReportResponse.model_validate(report)


@router.get(
    "/exit-workflows/{workflow_ref}/damage-report",
    response_model=DamageReportResponse,
    summary="Fetch the damage report (T13 step 7)",
)
async def get_damage_report(
    workflow_ref: WorkflowRef,
    workflows: WorkflowServiceDep,
    inspections: InspectionServiceDep,
) -> DamageReportResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref))
    return DamageReportResponse.model_validate(await inspections.get_report(workflow))


@router.post(
    "/exit-workflows/{workflow_ref}/damage-report/tenant-review",
    response_model=DamageReportResponse,
    summary="Tenant acknowledges or disputes the damage report",
)
async def tenant_review(
    workflow_ref: WorkflowRef,
    payload: TenantReviewRequest,
    workflows: WorkflowServiceDep,
    inspections: InspectionServiceDep,
) -> DamageReportResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    report = await inspections.get_report(workflow)
    await inspections.tenant_review(
        report, workflow, decision=payload.decision, note=payload.note
    )
    return DamageReportResponse.model_validate(report)


@router.post(
    "/exit-workflows/{workflow_ref}/damage-report/resolve-dispute",
    response_model=DamageReportResponse,
    summary="Owner or administrator resolves a disputed report",
)
async def resolve_dispute(
    workflow_ref: WorkflowRef,
    payload: ResolveDisputeRequest,
    workflows: WorkflowServiceDep,
    inspections: InspectionServiceDep,
) -> DamageReportResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    report = await inspections.get_report(workflow)
    await inspections.resolve_dispute(report, workflow, resolution_note=payload.resolution_note)
    return DamageReportResponse.model_validate(report)
