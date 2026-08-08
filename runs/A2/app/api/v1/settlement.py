"""Deposit settlement endpoints (SRS O16, T13 step 9)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import (
    IdempotencyKeyDep,
    LockedWorkflowDep,
    PortsDep,
    ServicesDep,
    WorkflowDep,
    require_roles,
)
from app.api.idempotency import IdempotencyGuard
from app.api.presenters import settlement as present_settlement
from app.core.context import RequestContext
from app.domain.enums import ActorRole
from app.schemas.settlement import (
    FinaliseSettlementRequest,
    PayDepositRequest,
    ReopenReviewRequest,
    SettlementPreview,
    SettlementResponse,
)

router = APIRouter(prefix="/exit-workflows/{workflow_id}/settlement", tags=["settlement"])

OwnerCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.OWNER))]
PartyCtx = Annotated[
    RequestContext, Depends(require_roles(ActorRole.TENANT, ActorRole.OWNER))
]


@router.get(
    "/preview",
    response_model=SettlementPreview,
    summary="Live projection of deposit minus damage",
    description=(
        "Recomputed on every read while damage review is open, so the owner always sees "
        "the effect of the latest adjustments and dispute outcomes."
    ),
)
async def preview_settlement(
    workflow: WorkflowDep, services: ServicesDep, ctx: PartyCtx
) -> SettlementPreview:
    return await services.settlements.preview(workflow)


@router.get("", response_model=SettlementResponse, summary="Fetch the settlement")
async def get_settlement(
    workflow: WorkflowDep, services: ServicesDep, ctx: PartyCtx
) -> SettlementResponse:
    return present_settlement(await services.settlements.get(workflow))


@router.post(
    "/finalise",
    response_model=SettlementResponse,
    summary="Finalise the deductions",
    description=(
        "Closes damage review and freezes the arithmetic. Rejected while any tenant "
        "dispute is unresolved. Send `expected_net_refund` to guard against acting on "
        "figures that have since changed."
    ),
)
async def finalise_settlement(
    workflow: LockedWorkflowDep,
    payload: FinaliseSettlementRequest,
    services: ServicesDep,
    ctx: OwnerCtx,
) -> SettlementResponse:
    settlement = await services.settlements.finalise(workflow, payload, ctx)
    return present_settlement(settlement)


@router.post(
    "/pay",
    response_model=SettlementResponse,
    summary="Pay the deposit (deposit minus damage)",
    description=(
        "SRS O16: the owner releases the refund. Requires an `Idempotency-Key` header. "
        "The payout is submitted to the payment provider after this transaction "
        "commits; the settlement completes -- and the Exit NOC is generated -- when the "
        "provider confirms. When the net refund is zero, use "
        "`payout_method=OFFSET_ONLY` and the settlement completes immediately."
    ),
)
async def pay_deposit(
    request: Request,
    workflow: LockedWorkflowDep,
    payload: PayDepositRequest,
    services: ServicesDep,
    ports: PortsDep,
    ctx: OwnerCtx,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> SettlementResponse:
    guard = IdempotencyGuard(services.uow.session, ports.clock)
    replay = await guard.begin(
        key=idempotency_key,
        endpoint="POST /exit-workflows/{workflow_id}/settlement/pay",
        actor_id=ctx.principal.actor_id,
        workflow_id=workflow.id,
        body=await request.body(),
        required=True,
    )
    if replay is not None:
        response.headers["Idempotent-Replay"] = "true"
        return SettlementResponse.model_validate(replay.body)

    settlement = await services.settlements.pay_deposit(
        workflow, payload, ctx, idempotency_key=idempotency_key
    )
    body = present_settlement(settlement)
    guard.complete(status_code=200, body=body.model_dump(mode="json"))
    return body


@router.post(
    "/reopen",
    response_model=SettlementResponse,
    summary="Reopen damage review before paying",
)
async def reopen_review(
    workflow: LockedWorkflowDep,
    payload: ReopenReviewRequest,
    services: ServicesDep,
    ctx: OwnerCtx,
) -> SettlementResponse:
    settlement = await services.settlements.reopen_review(workflow, payload.reason, ctx)
    return present_settlement(settlement)
