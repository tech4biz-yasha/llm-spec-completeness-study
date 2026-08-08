"""Inspection endpoints (SRS O15) and damage review (T13 step 8, O16)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import ContextDep, LockedWorkflowDep, ServicesDep, WorkflowDep, require_roles
from app.api.presenters import damage_item as present_damage_item
from app.api.presenters import inspection as present_inspection
from app.core.context import RequestContext
from app.domain.enums import ActorRole
from app.schemas.inspection import (
    AdjustDamageItemRequest,
    DamageItemResponse,
    InspectionResponse,
    ProposeSlotsRequest,
    RaiseDisputeRequest,
    RescheduleRequest,
    ResolveDisputeRequest,
    SelectSlotRequest,
    SubmitInspectionReportRequest,
)

router = APIRouter(prefix="/exit-workflows/{workflow_id}", tags=["inspection"])

AgencyCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.INSPECTION_AGENCY))]
OwnerCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.OWNER))]
TenantCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.TENANT))]
PartyCtx = Annotated[
    RequestContext, Depends(require_roles(ActorRole.TENANT, ActorRole.OWNER))
]
SchedulingCtx = Annotated[
    RequestContext,
    Depends(require_roles(ActorRole.TENANT, ActorRole.OWNER, ActorRole.INSPECTION_AGENCY)),
]


@router.get(
    "/inspection",
    response_model=InspectionResponse,
    summary="Fetch the inspection and its damage assessment",
)
async def get_inspection(
    workflow: WorkflowDep, services: ServicesDep
) -> InspectionResponse:
    inspection = await services.inspections.get_for_workflow(workflow)
    return present_inspection(inspection)


@router.post(
    "/inspection/slots",
    response_model=InspectionResponse,
    summary="Agency proposes available inspection dates",
    description=(
        "SRS O15: the agency responds with available dates. Re-proposing replaces the "
        "previous offer, so the parties can never select a stale slot."
    ),
)
async def propose_slots(
    workflow: LockedWorkflowDep,
    payload: ProposeSlotsRequest,
    services: ServicesDep,
    ctx: AgencyCtx,
) -> InspectionResponse:
    inspection = await services.inspections.propose_slots(workflow, payload, ctx=ctx)
    return present_inspection(inspection)


@router.post(
    "/inspection/schedule",
    response_model=InspectionResponse,
    summary="Owner or tenant selects an inspection date",
    description="SRS T13 step 7 / O15.",
)
async def select_slot(
    workflow: LockedWorkflowDep,
    payload: SelectSlotRequest,
    services: ServicesDep,
    ctx: PartyCtx,
) -> InspectionResponse:
    inspection = await services.inspections.select_slot(workflow, payload, ctx=ctx)
    return present_inspection(inspection)


@router.post(
    "/inspection/reschedule",
    response_model=InspectionResponse,
    summary="Return a scheduled inspection to date selection",
)
async def reschedule(
    workflow: LockedWorkflowDep,
    payload: RescheduleRequest,
    services: ServicesDep,
    ctx: SchedulingCtx,
) -> InspectionResponse:
    inspection = await services.inspections.reschedule(workflow, payload, ctx=ctx)
    return present_inspection(inspection)


@router.post(
    "/inspection/report",
    response_model=InspectionResponse,
    summary="Agency submits the damage report",
    description=(
        "SRS O16: the agency uploads the damage report with photos; the system then "
        "opens damage review and begins the tenant's dispute window automatically."
    ),
)
async def submit_report(
    workflow: LockedWorkflowDep,
    payload: SubmitInspectionReportRequest,
    services: ServicesDep,
    ctx: AgencyCtx,
) -> InspectionResponse:
    inspection = await services.inspections.submit_report(workflow, payload, ctx=ctx)
    return present_inspection(inspection)


@router.patch(
    "/damage-items/{item_id}",
    response_model=DamageItemResponse,
    summary="Owner adjusts an assessed charge",
    description=(
        "SRS T13 step 8. The approved charge may be reduced or waived but never raised "
        "above the agency's assessment."
    ),
)
async def adjust_damage_item(
    workflow: LockedWorkflowDep,
    item_id: uuid.UUID,
    payload: AdjustDamageItemRequest,
    services: ServicesDep,
    ctx: OwnerCtx,
) -> DamageItemResponse:
    item = await services.inspections.adjust_damage_item(
        workflow, item_id, payload, ctx=ctx
    )
    return present_damage_item(item)


@router.post(
    "/damage-items/{item_id}/dispute",
    response_model=DamageItemResponse,
    summary="Tenant disputes an assessed charge",
    description=(
        "Allowed within the dispute window that opens when the damage report lands. "
        "An unresolved dispute blocks settlement finalisation."
    ),
)
async def raise_dispute(
    workflow: LockedWorkflowDep,
    item_id: uuid.UUID,
    payload: RaiseDisputeRequest,
    services: ServicesDep,
    ctx: TenantCtx,
) -> DamageItemResponse:
    item = await services.inspections.raise_dispute(workflow, item_id, payload, ctx=ctx)
    return present_damage_item(item)


@router.post(
    "/damage-items/{item_id}/dispute/resolve",
    response_model=DamageItemResponse,
    summary="Owner resolves a dispute",
)
async def resolve_dispute(
    workflow: LockedWorkflowDep,
    item_id: uuid.UUID,
    payload: ResolveDisputeRequest,
    services: ServicesDep,
    ctx: OwnerCtx,
) -> DamageItemResponse:
    item = await services.inspections.resolve_dispute(workflow, item_id, payload, ctx=ctx)
    return present_damage_item(item)


@router.post(
    "/inspection/re-inspect",
    response_model=InspectionResponse,
    summary="Escalate a contested assessment to a new inspection round",
)
async def request_reinspection(
    workflow: LockedWorkflowDep,
    payload: RescheduleRequest,
    services: ServicesDep,
    ctx: OwnerCtx,
) -> InspectionResponse:
    inspection = await services.inspections.request_reinspection(workflow, payload, ctx=ctx)
    return present_inspection(inspection)
