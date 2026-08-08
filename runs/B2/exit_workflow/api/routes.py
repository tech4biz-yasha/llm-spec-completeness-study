"""HTTP surface — exactly the five endpoints in api.yaml, no more.

    POST /exit-workflows                          201 / 409 / 422
    POST /exit-workflows/{id}/schedule-inspection 200 / 409
    POST /exit-workflows/{id}/inspection-report   200
    POST /exit-workflows/{id}/confirm-damage      200 / 409
    POST /exit-workflows/{id}/settle              200 / 409 / 501
"""

from __future__ import annotations

from fastapi import APIRouter, status

from ..services.initiation import InitiationCommand
from ..services.inspection import InspectionReport
from .deps import ContainerDep, PrincipalDep
from .schemas import (
    ErrorResponse,
    InitiateExitRequest,
    InitiateExitResponse,
    InspectionReportRequest,
    ScheduleInspectionRequest,
    SettlementResponse,
    WorkflowStatusResponse,
)

router = APIRouter(prefix="/exit-workflows", tags=["exit-workflow"])

_ERROR = {"model": ErrorResponse}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=InitiateExitResponse,
    responses={409: _ERROR, 422: _ERROR, 501: _ERROR},
    summary="Initiate an exit workflow",
)
async def initiate_exit(
    payload: InitiateExitRequest, container: ContainerDep, principal: PrincipalDep
) -> InitiateExitResponse:
    # api.yaml authz: tenant, own active contract only (rules.yaml#EXIT-01).
    workflow = await container.initiation.initiate(
        InitiationCommand(
            contract_id=payload.contract_id,
            move_out_date=payload.move_out_date,
            reason=payload.reason,
            documents=[document.model_dump(mode="json") for document in payload.documents],
        ),
        principal,
    )
    return InitiateExitResponse(workflow_id=workflow.id, status=workflow.status)


@router.post(
    "/{workflow_id}/schedule-inspection",
    response_model=WorkflowStatusResponse,
    responses={409: _ERROR},
    summary="Owner schedules the exit inspection",
)
async def schedule_inspection(
    workflow_id: str,
    payload: ScheduleInspectionRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> WorkflowStatusResponse:
    # api.yaml authz: owner. rules.yaml#EXIT-05.
    workflow = await container.inspection.schedule_inspection(
        workflow_id, principal, scheduled_for=payload.scheduled_for
    )
    return WorkflowStatusResponse(workflow_id=workflow.id, status=workflow.status)


@router.post(
    "/{workflow_id}/inspection-report",
    response_model=WorkflowStatusResponse,
    responses={409: _ERROR},
    summary="Inspection agency files the damage assessment",
)
async def submit_inspection_report(
    workflow_id: str,
    payload: InspectionReportRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> WorkflowStatusResponse:
    # api.yaml authz: inspection_agency. rules.yaml#EXIT-06.
    workflow = await container.inspection.submit_report(
        workflow_id,
        InspectionReport(
            damage_amount=payload.damage_amount,
            photos=[photo.model_dump(mode="json") for photo in payload.photos],
        ),
        principal,
    )
    return WorkflowStatusResponse(workflow_id=workflow.id, status=workflow.status)


@router.post(
    "/{workflow_id}/confirm-damage",
    response_model=WorkflowStatusResponse,
    responses={409: _ERROR},
    summary="Owner confirms the damage assessment",
)
async def confirm_damage(
    workflow_id: str, container: ContainerDep, principal: PrincipalDep
) -> WorkflowStatusResponse:
    # api.yaml authz: owner. rules.yaml#EXIT-06 — required before settlement.
    workflow = await container.inspection.confirm_damage(workflow_id, principal)
    return WorkflowStatusResponse(workflow_id=workflow.id, status=workflow.status)


@router.post(
    "/{workflow_id}/settle",
    response_model=SettlementResponse,
    responses={409: _ERROR, 501: _ERROR},
    summary="Settle the deposit, then issue the NOC and complete",
)
async def settle(
    workflow_id: str, container: ContainerDep, principal: PrincipalDep
) -> SettlementResponse:
    # api.yaml authz: system|owner. algorithm.md steps 9-13; 501
    # SPEC_UNRESOLVED_R8 when confirmed damage exceeds the deposit.
    result = await container.settlement.settle(workflow_id, principal)
    return SettlementResponse.from_minor_units(
        refund_amount_minor=result.refund_amount_minor,
        payment_id=result.payment_id,
        status=result.status,
    )
