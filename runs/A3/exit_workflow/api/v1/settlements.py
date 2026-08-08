"""O16 — deduction finalisation and the 'Pay Deposit' action."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse

from exit_workflow.api.deps import (
    IdempotencyDep,
    IdempotencyKeyDep,
    InspectionServiceDep,
    PrincipalDep,
    SettlementServiceDep,
    WorkflowServiceDep,
)
from exit_workflow.api.schemas.settlement import (
    FinalizeDeductionRequest,
    PayDepositRequest,
    ReconcileSettlementRequest,
    SettlementPreviewResponse,
    SettlementResponse,
)
from exit_workflow.api.v1.common import WORKFLOW_PATH_DESCRIPTION, workflow_identifier
from exit_workflow.core.errors import NotFoundError
from exit_workflow.services.idempotency import hash_request

router = APIRouter(prefix="/exit-workflows/{workflow_ref}/settlement", tags=["settlement"])

WorkflowRef = Annotated[str, Path(description=WORKFLOW_PATH_DESCRIPTION)]

PAY_SCOPE = "settlement.pay"


@router.get("", response_model=SettlementResponse, summary="Fetch the deposit settlement")
async def get_settlement(
    workflow_ref: WorkflowRef,
    workflows: WorkflowServiceDep,
    settlements: SettlementServiceDep,
) -> SettlementResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref))
    return SettlementResponse.model_validate(await settlements.get(workflow))


@router.get(
    "/preview",
    response_model=SettlementPreviewResponse,
    summary="Preview deposit minus damage before finalising",
)
async def preview_settlement(
    workflow_ref: WorkflowRef,
    workflows: WorkflowServiceDep,
    settlements: SettlementServiceDep,
    inspections: InspectionServiceDep,
) -> SettlementPreviewResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref))
    try:
        report = await inspections.get_report(workflow)
    except NotFoundError:
        report = None
    breakdown = await settlements.preview(workflow, report)
    return SettlementPreviewResponse(
        currency=breakdown.currency,
        security_deposit_amount=breakdown.security_deposit_amount,
        total_deduction_amount=breakdown.total_deduction_amount,
        refund_amount=breakdown.refund_amount,
        balance_due_from_tenant=breakdown.balance_due_from_tenant,
        is_final=bool(report and report.finalized_total is not None),
        damage_report_id=report.id if report else None,
    )


@router.post(
    "/finalize",
    response_model=SettlementResponse,
    summary="Owner finalises the damage deduction",
    description=(
        "Computes deposit minus damage and moves the exit to SETTLEMENT_PENDING. The "
        "deduction may be reduced below the agency's assessment (with a reason) but "
        "never raised above it."
    ),
)
async def finalize_deduction(
    workflow_ref: WorkflowRef,
    payload: FinalizeDeductionRequest,
    workflows: WorkflowServiceDep,
    settlements: SettlementServiceDep,
    inspections: InspectionServiceDep,
) -> SettlementResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    report = await inspections.get_report(workflow)
    settlement = await settlements.finalize_deduction(
        workflow,
        report,
        deduction_amount=payload.deduction_amount,
        adjustment_reason=payload.adjustment_reason,
        payout_method=payload.payout_method,
        payout_destination_token=payload.payout_destination_token,
        payout_destination_masked=payload.payout_destination_masked,
    )
    return SettlementResponse.model_validate(settlement)


@router.post(
    "/pay",
    response_model=SettlementResponse,
    summary="Pay Deposit — release the refund and auto-generate the Exit NOC",
    description=(
        "Requires an Idempotency-Key header. On success the settlement is PAID, the "
        "Exit NOC is generated and the workflow is COMPLETE (releasing the BR-1 lock). "
        "A declined payout returns 502 and leaves the settlement payable; retry with a "
        "new Idempotency-Key."
    ),
    responses={
        502: {"description": "Payout declined or the gateway outcome is unknown"},
        409: {"description": "Already paid, or a payout is awaiting reconciliation"},
    },
)
async def pay_deposit(
    request: Request,
    workflow_ref: WorkflowRef,
    payload: PayDepositRequest,
    key: IdempotencyKeyDep,
    idempotency: IdempotencyDep,
    principal: PrincipalDep,
    workflows: WorkflowServiceDep,
    settlements: SettlementServiceDep,
) -> JSONResponse:
    request_hash = hash_request(
        {"workflow_ref": workflow_ref.upper(), "body": payload.model_dump(mode="json")}
    )
    replay = await idempotency.begin(
        scope=PAY_SCOPE, key=key, request_hash=request_hash, principal_id=principal.subject_id
    )
    if replay is not None:
        return JSONResponse(
            replay.body,
            status_code=replay.status_code,
            headers={"Idempotent-Replay": "true"},
        )

    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    settlement = await settlements.get(workflow, for_update=True)
    outcome = await settlements.pay(
        workflow,
        settlement,
        idempotency_key=key,
        payout_destination_token=payload.payout_destination_token,
        payout_destination_masked=payload.payout_destination_masked,
    )

    settlement_body = SettlementResponse.model_validate(outcome.settlement).model_dump(mode="json")
    if outcome.succeeded:
        status_code, body = 200, settlement_body
    else:
        status_code = 502
        code = "payment_indeterminate" if outcome.indeterminate else "payment_failed"
        body: dict[str, Any] = {
            "type": f"https://errors.meridian.ae/exit-workflow/{code}",
            "title": "Deposit payout was not completed",
            "status": status_code,
            "code": code,
            "detail": outcome.failure_message or "The payout could not be completed.",
            "instance": str(request.url.path),
            "failure_code": outcome.failure_code,
            "transaction_id": str(outcome.transaction.id),
            "settlement": settlement_body,
        }

    # Recorded before commit, so the replay of this key returns exactly this.
    await idempotency.complete(scope=PAY_SCOPE, key=key, status_code=status_code, body=body)
    media_type = "application/json" if outcome.succeeded else "application/problem+json"
    return JSONResponse(body, status_code=status_code, media_type=media_type)


@router.post(
    "/reconcile",
    response_model=SettlementResponse,
    summary="Administrator resolves a payout with an unknown outcome",
)
async def reconcile_settlement(
    workflow_ref: WorkflowRef,
    payload: ReconcileSettlementRequest,
    workflows: WorkflowServiceDep,
    settlements: SettlementServiceDep,
) -> SettlementResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    settlement = await settlements.get(workflow, for_update=True)
    updated = await settlements.reconcile(
        workflow,
        settlement,
        transaction_id=payload.transaction_id,
        succeeded=payload.succeeded,
        gateway_reference=payload.gateway_reference,
        note=payload.note,
    )
    return SettlementResponse.model_validate(updated)
