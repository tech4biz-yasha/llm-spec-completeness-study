"""Routes. One per api.yaml path; nothing beyond api.yaml is exposed.

Notably absent: an owner-dispute endpoint. rules.yaml#EXIT-06 mentions a dispute, but
api.yaml declares no path, no state exists for it in states.yaml, and no error code
covers it — so no route is invented. ``ExitWorkflowService.dispute_damage`` raises
SpecUnresolved. See blockers.md#B-1.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Path, status

from ..schemas import (
    ErrorResponse,
    InitiateExitRequest,
    InitiateExitResponse,
    InspectionReportRequest,
    SettleResponse,
    WorkflowStatusResponse,
)
from ..services.identity import Principal
from ..services.workflow import ExitWorkflowService
from .deps import get_principal, get_service

router = APIRouter(prefix="/exit-workflows", tags=["exit-workflow"])

WorkflowId = Path(min_length=1, max_length=32, description="EX-YYYYMMDD-NNNNN")

_ERRORS = {
    "409": {"model": ErrorResponse},
    "422": {"model": ErrorResponse},
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=InitiateExitResponse,
    responses=_ERRORS,
    summary="Initiate an exit workflow",
)
def initiate_exit(
    payload: InitiateExitRequest,
    background: BackgroundTasks,
    service: ExitWorkflowService = Depends(get_service),
    principal: Principal = Depends(get_principal),
) -> InitiateExitResponse:
    """api.yaml POST /exit-workflows — authz: tenant, own active contract only."""
    result = service.initiate(
        principal=principal,
        contract_id=payload.contract_id,
        move_out_date=payload.move_out_date,
        reason=payload.reason,
        documents=payload.documents,
    )
    # rules.yaml#EXIT-04 / algorithm.md#5 — the notification is emitted after the
    # initiation transaction commits, which is to say after this response is produced.
    background.add_task(service.notify_owner, result.workflow_id, result.outbox_event_id)
    return InitiateExitResponse(workflow_id=result.workflow_id, status=result.status)


@router.post(
    "/{workflow_id}/schedule-inspection",
    response_model=WorkflowStatusResponse,
    responses=_ERRORS,
    summary="Owner schedules the move-out inspection",
)
def schedule_inspection(
    workflow_id: str = WorkflowId,
    service: ExitWorkflowService = Depends(get_service),
    principal: Principal = Depends(get_principal),
) -> WorkflowStatusResponse:
    """api.yaml POST /{id}/schedule-inspection — authz: owner. 409 WRONG_STATE."""
    state = service.schedule_inspection(workflow_id, principal=principal)
    return WorkflowStatusResponse(workflow_id=workflow_id, status=state)


@router.post(
    "/{workflow_id}/inspection-report",
    response_model=WorkflowStatusResponse,
    responses=_ERRORS,
    summary="Inspection agency files the damage assessment",
)
def inspection_report(
    payload: InspectionReportRequest,
    workflow_id: str = WorkflowId,
    service: ExitWorkflowService = Depends(get_service),
    principal: Principal = Depends(get_principal),
) -> WorkflowStatusResponse:
    """api.yaml POST /{id}/inspection-report — authz: inspection_agency."""
    state = service.submit_inspection_report(
        workflow_id,
        principal=principal,
        damage_amount=payload.damage_amount,
        photos=payload.photos,
    )
    return WorkflowStatusResponse(workflow_id=workflow_id, status=state)


@router.post(
    "/{workflow_id}/confirm-damage",
    response_model=WorkflowStatusResponse,
    responses=_ERRORS,
    summary="Owner confirms the damage assessment",
)
def confirm_damage(
    workflow_id: str = WorkflowId,
    service: ExitWorkflowService = Depends(get_service),
    principal: Principal = Depends(get_principal),
) -> WorkflowStatusResponse:
    """api.yaml POST /{id}/confirm-damage — authz: owner. 409 WRONG_STATE."""
    state = service.confirm_damage(workflow_id, principal=principal)
    return WorkflowStatusResponse(workflow_id=workflow_id, status=state)


@router.post(
    "/{workflow_id}/settle",
    response_model=SettleResponse,
    responses={**_ERRORS, "501": {"model": ErrorResponse}},
    summary="Settle the deposit, issue the NOC and complete the workflow",
)
def settle(
    workflow_id: str = WorkflowId,
    service: ExitWorkflowService = Depends(get_service),
    principal: Principal = Depends(get_principal),
) -> SettleResponse:
    """api.yaml POST /{id}/settle — authz: system|owner.

    409 WRONG_STATE | PAYMENT_PENDING, 501 SPEC_UNRESOLVED_R8 (damage > deposit).
    """
    result = service.settle(workflow_id, principal=principal)
    return SettleResponse(
        workflow_id=result.workflow_id,
        refund_amount=result.refund_amount,
        payment_id=result.payment_id,
        status=result.status,
    )
