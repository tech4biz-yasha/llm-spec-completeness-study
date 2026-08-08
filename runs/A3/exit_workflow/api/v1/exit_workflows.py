"""T13 — exit initiation, submission, owner decision, cancellation."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response, status

from exit_workflow.api.deps import PrincipalDep, WorkflowServiceDep
from exit_workflow.api.presenters import step_views, workflow_detail, workflow_summary
from exit_workflow.api.schemas.common import Page, PageMeta, StepView
from exit_workflow.api.schemas.workflow import (
    CancelWorkflowRequest,
    ExitWorkflowDetail,
    ExitWorkflowSummary,
    InitiateExitRequest,
    OwnerDecisionRequest,
    TransitionResponse,
)
from exit_workflow.api.v1.common import WORKFLOW_PATH_DESCRIPTION, workflow_identifier
from exit_workflow.core.errors import ValidationError
from exit_workflow.domain.enums import ExitWorkflowStatus

router = APIRouter(prefix="/exit-workflows", tags=["exit-workflows"])

WorkflowRef = Annotated[str, Path(description=WORKFLOW_PATH_DESCRIPTION)]


@router.post(
    "",
    response_model=ExitWorkflowDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate an exit (T13 steps 1-4)",
    description=(
        "Creates the exit workflow and allocates its Workflow ID. The property, "
        "owner and security deposit are read from the contract, never from the "
        "request. Blocked by BR-1 if the property or tenant already has a live "
        "exit workflow."
    ),
)
async def initiate_exit(
    payload: InitiateExitRequest,
    service: WorkflowServiceDep,
    principal: PrincipalDep,
    response: Response,
) -> ExitWorkflowDetail:
    workflow = await service.initiate(
        contract_id=payload.contract_id,
        move_out_date=payload.move_out_date,
        reason=payload.reason,
        reason_details=payload.reason_details,
    )
    response.headers["Location"] = f"/api/v1/exit-workflows/{workflow.reference}"
    return workflow_detail(workflow, principal)


@router.get("", response_model=Page[ExitWorkflowSummary], summary="List exit workflows")
async def list_exit_workflows(
    service: WorkflowServiceDep,
    status_filter: Annotated[
        list[ExitWorkflowStatus] | None, Query(alias="status", description="Repeatable")
    ] = None,
    property_id: uuid.UUID | None = None,
    contract_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    active_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ExitWorkflowSummary]:
    rows, total = await service.list(
        statuses=status_filter,
        property_id=property_id,
        contract_id=contract_id,
        tenant_id=tenant_id,
        owner_id=owner_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return Page[ExitWorkflowSummary](
        items=[workflow_summary(row) for row in rows],
        meta=PageMeta(total=total, limit=limit, offset=offset, returned=len(rows)),
    )


@router.get(
    "/{workflow_ref}",
    response_model=ExitWorkflowDetail,
    summary="Fetch one exit workflow with its 10-step progress",
)
async def get_exit_workflow(
    workflow_ref: WorkflowRef,
    service: WorkflowServiceDep,
    principal: PrincipalDep,
) -> ExitWorkflowDetail:
    workflow = await service.get(workflow_identifier(workflow_ref))
    return workflow_detail(workflow, principal)


@router.get(
    "/{workflow_ref}/steps",
    response_model=list[StepView],
    summary="The ten T13 steps and their state",
)
async def get_steps(workflow_ref: WorkflowRef, service: WorkflowServiceDep) -> list[StepView]:
    workflow = await service.get(workflow_identifier(workflow_ref))
    return step_views(workflow)


@router.get(
    "/{workflow_ref}/timeline",
    response_model=list[TransitionResponse],
    summary="Status history",
)
async def get_timeline(
    workflow_ref: WorkflowRef, service: WorkflowServiceDep
) -> list[TransitionResponse]:
    workflow = await service.get(workflow_identifier(workflow_ref))
    return [TransitionResponse.model_validate(t) for t in await service.timeline(workflow)]


@router.post(
    "/{workflow_ref}/submit",
    response_model=ExitWorkflowDetail,
    summary="Submit the request and notify the owner (T13 step 5)",
)
async def submit_exit_workflow(
    workflow_ref: WorkflowRef,
    service: WorkflowServiceDep,
    principal: PrincipalDep,
) -> ExitWorkflowDetail:
    workflow = await service.get(workflow_identifier(workflow_ref), for_update=True)
    await service.submit(workflow)
    return workflow_detail(workflow, principal)


@router.post(
    "/{workflow_ref}/owner-decision",
    response_model=ExitWorkflowDetail,
    summary="Owner approves or rejects the exit",
)
async def owner_decision(
    workflow_ref: WorkflowRef,
    payload: OwnerDecisionRequest,
    service: WorkflowServiceDep,
    principal: PrincipalDep,
) -> ExitWorkflowDetail:
    workflow = await service.get(workflow_identifier(workflow_ref), for_update=True)
    if payload.decision == "APPROVE":
        await service.approve(workflow, note=payload.reason)
    else:
        if not payload.reason:
            raise ValidationError(
                "A reason is required when rejecting an exit request.",
                extra={"field": "reason"},
            )
        await service.reject(workflow, reason=payload.reason)
    return workflow_detail(workflow, principal)


@router.post(
    "/{workflow_ref}/cancel",
    response_model=ExitWorkflowDetail,
    summary="Cancel the exit workflow",
)
async def cancel_exit_workflow(
    workflow_ref: WorkflowRef,
    payload: CancelWorkflowRequest,
    service: WorkflowServiceDep,
    principal: PrincipalDep,
) -> ExitWorkflowDetail:
    workflow = await service.get(workflow_identifier(workflow_ref), for_update=True)
    await service.cancel(workflow, reason=payload.reason)
    return workflow_detail(workflow, principal)
