"""Exit workflow endpoints (SRS T13 steps 1-6 and 11, O15 approval)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    ContextDep,
    PaginationDep,
    ServicesDep,
    WorkflowDep,
    require_roles,
)
from app.api.presenters import timeline_entry, workflow_detail, workflow_summary
from app.core.context import RequestContext
from app.core.pagination import Cursor, Page
from app.domain.enums import ActorRole, ExitWorkflowState
from app.models.exit_workflow import ExitWorkflow
from app.repositories.support import AuditRepository
from app.schemas.exit_workflow import (
    CancelRequest,
    CompleteRequest,
    ExitWorkflowDetail,
    ExitWorkflowSummary,
    InitiateExitRequest,
    OwnerApproveRequest,
    OwnerRejectRequest,
    SubmitExitRequest,
    TimelineEntry,
    UpdateDraftRequest,
)
from app.services.factory import Services

router = APIRouter(prefix="/exit-workflows", tags=["exit-workflows"])

TenantCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.TENANT))]
OwnerCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.OWNER))]
PartyCtx = Annotated[
    RequestContext, Depends(require_roles(ActorRole.TENANT, ActorRole.OWNER))
]
AdminCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.ADMIN))]


async def _detail(
    services: Services, workflow: ExitWorkflow, ctx: RequestContext
) -> ExitWorkflowDetail:
    count = await services.documents_repo.count_active(workflow.id)
    return workflow_detail(workflow, ctx, document_count=count)


@router.post(
    "",
    response_model=ExitWorkflowDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate an exit workflow",
    description=(
        "SRS T13 steps 1-3. Creates a DRAFT. The move-out date and reason may be "
        "supplied here or patched in later; documents are attached separately. The "
        "Workflow ID (`reference`) is allocated at submission, per T13 step 5."
    ),
)
async def initiate_exit(
    payload: InitiateExitRequest, services: ServicesDep, ctx: PartyCtx
) -> ExitWorkflowDetail:
    workflow = await services.workflows.initiate(payload, ctx)
    return await _detail(services, workflow, ctx)


@router.get(
    "",
    response_model=Page[ExitWorkflowSummary],
    summary="List exit workflows visible to the caller",
    description=(
        "Scoped by role: a tenant sees their own exits, an owner theirs, an inspection "
        "agency the ones it was engaged for."
    ),
)
async def list_exits(
    services: ServicesDep,
    ctx: ContextDep,
    pagination: PaginationDep,
    state: Annotated[list[ExitWorkflowState] | None, Query()] = None,
    property_id: Annotated[uuid.UUID | None, Query()] = None,
    active_only: Annotated[
        bool, Query(description="Only workflows currently holding the BR-1 lock")
    ] = False,
) -> Page[ExitWorkflowSummary]:
    cursor, limit = pagination
    items, has_more = await services.workflows.list_for_principal(
        ctx,
        states=state,
        property_id=property_id,
        active_only=active_only,
        cursor=cursor,
        limit=limit,
    )
    next_cursor = (
        Cursor(created_at=items[-1].created_at, id=items[-1].id).encode()
        if has_more and items
        else None
    )
    return Page[ExitWorkflowSummary](
        items=[workflow_summary(w) for w in items],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get(
    "/by-reference/{reference}",
    response_model=ExitWorkflowDetail,
    summary="Look up an exit workflow by its Workflow ID",
)
async def get_by_reference(
    reference: str, services: ServicesDep, ctx: ContextDep
) -> ExitWorkflowDetail:
    workflow = await services.workflows.get_by_reference(reference, ctx)
    return await _detail(services, workflow, ctx)


@router.get("/{workflow_id}", response_model=ExitWorkflowDetail, summary="Fetch one exit workflow")
async def get_exit(
    workflow: WorkflowDep, services: ServicesDep, ctx: ContextDep
) -> ExitWorkflowDetail:
    return await _detail(services, workflow, ctx)


@router.get(
    "/{workflow_id}/timeline",
    response_model=list[TimelineEntry],
    summary="State-change history",
)
async def get_timeline(workflow: WorkflowDep, services: ServicesDep) -> list[TimelineEntry]:
    transitions = await services.workflows_repo.transitions_for(workflow.id)
    return [timeline_entry(t) for t in transitions]


@router.patch(
    "/{workflow_id}",
    response_model=ExitWorkflowDetail,
    summary="Update a draft exit request",
    description="SRS T13 steps 2-3: move-out date and reason entry.",
)
async def update_draft(
    workflow_id: uuid.UUID,
    payload: UpdateDraftRequest,
    services: ServicesDep,
    ctx: TenantCtx,
) -> ExitWorkflowDetail:
    workflow = await services.workflows.update_draft(workflow_id, payload, ctx)
    return await _detail(services, workflow, ctx)


@router.post(
    "/{workflow_id}/submit",
    response_model=ExitWorkflowDetail,
    summary="Submit the exit request",
    description=(
        "SRS T13 steps 5-6: allocates the Workflow ID and notifies the owner. Requires "
        "a valid move-out date, a reason, and the configured supporting documents."
    ),
)
async def submit_exit(
    workflow_id: uuid.UUID,
    payload: SubmitExitRequest,
    services: ServicesDep,
    ctx: TenantCtx,
) -> ExitWorkflowDetail:
    workflow = await services.workflows.submit(workflow_id, payload, ctx)
    return await _detail(services, workflow, ctx)


@router.post(
    "/{workflow_id}/withdraw",
    response_model=ExitWorkflowDetail,
    summary="Withdraw a submitted request back to draft",
)
async def withdraw_exit(
    workflow_id: uuid.UUID, services: ServicesDep, ctx: TenantCtx
) -> ExitWorkflowDetail:
    workflow = await services.workflows.withdraw(workflow_id, ctx)
    return await _detail(services, workflow, ctx)


@router.post(
    "/{workflow_id}/approve",
    response_model=ExitWorkflowDetail,
    summary="Owner approves the exit and engages an inspection agency",
    description=(
        "SRS O15: owner approves > the registered inspection agency is emailed the "
        "property details > the agency responds with available dates."
    ),
)
async def approve_exit(
    workflow_id: uuid.UUID,
    payload: OwnerApproveRequest,
    services: ServicesDep,
    ctx: OwnerCtx,
) -> ExitWorkflowDetail:
    workflow = await services.workflows.owner_approve(workflow_id, payload, ctx)
    return await _detail(services, workflow, ctx)


@router.post(
    "/{workflow_id}/reject",
    response_model=ExitWorkflowDetail,
    summary="Owner rejects the exit request",
)
async def reject_exit(
    workflow_id: uuid.UUID,
    payload: OwnerRejectRequest,
    services: ServicesDep,
    ctx: OwnerCtx,
) -> ExitWorkflowDetail:
    workflow = await services.workflows.owner_reject(workflow_id, payload, ctx)
    return await _detail(services, workflow, ctx)


@router.post(
    "/{workflow_id}/cancel",
    response_model=ExitWorkflowDetail,
    summary="Cancel the exit workflow",
    description="Releases the BR-1 lock: the property and tenant become contractable again.",
)
async def cancel_exit(
    workflow_id: uuid.UUID,
    payload: CancelRequest,
    services: ServicesDep,
    ctx: PartyCtx,
) -> ExitWorkflowDetail:
    workflow = await services.workflows.cancel(workflow_id, payload, ctx)
    return await _detail(services, workflow, ctx)


@router.post(
    "/{workflow_id}/complete",
    response_model=ExitWorkflowDetail,
    summary="Complete the exit workflow",
    description=(
        "SRS T13 step 11. Requires an issued NOC. Marking the workflow COMPLETE is what "
        "lifts the BR-1 block on new contracts for the property and the tenant."
    ),
)
async def complete_exit(
    workflow_id: uuid.UUID,
    payload: CompleteRequest,
    services: ServicesDep,
    ctx: PartyCtx,
) -> ExitWorkflowDetail:
    workflow = await services.workflows.complete(workflow_id, payload, ctx)
    return await _detail(services, workflow, ctx)


@router.get(
    "/{workflow_id}/audit-log",
    summary="Audit trail for one exit workflow",
    description="SRS A3. Restricted to administrators.",
)
async def get_audit_log(
    workflow_id: uuid.UUID,
    services: ServicesDep,
    ctx: AdminCtx,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    await services.workflows_repo.require(workflow_id)
    repo = AuditRepository(services.uow.session)
    entries = await repo.list_for_workflow(workflow_id, limit=limit, offset=offset)
    response.headers["Cache-Control"] = "no-store"
    return [
        {
            "id": entry.id,
            "occurred_at": entry.occurred_at.isoformat(),
            "action": entry.action,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "actor_id": str(entry.actor_id) if entry.actor_id else None,
            "actor_role": entry.actor_role.value,
            "from_state": entry.from_state,
            "to_state": entry.to_state,
            "changes": entry.changes,
            "context": entry.context,
            "request_id": entry.request_id,
            "ip_address": entry.ip_address,
            "retention_until": entry.retention_until.isoformat(),
        }
        for entry in entries
    ]
