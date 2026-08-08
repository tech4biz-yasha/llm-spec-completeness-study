"""Endpoints, exactly the five in api.yaml.

No route exists here that api.yaml does not define. In particular there is no
dispute endpoint: rules.yaml#EXIT-06 grants the owner one dispute "routed to
admin review", but states.yaml has no state for it, no transition into or out of
it, and api.yaml has no path — so there is nothing to build against
(blockers.md#B-5).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Path, status

from exit_workflow.api.dependencies import (
    ClockDep,
    DispatcherDep,
    GatewayDep,
    NocStorageDep,
    PrincipalDep,
    ReasonsDep,
    SessionDep,
    SettingsDep,
)
from exit_workflow.api.schemas import (
    ErrorResponse,
    InitiateExitRequest,
    InitiateExitResponse,
    InspectionReportRequest,
    SettlementResponse,
    WorkflowStateResponse,
)
from exit_workflow.domain.errors import ExitWorkflowError
from exit_workflow.domain.states import State
from exit_workflow.services.damage import DamageService
from exit_workflow.services.initiation import ExitInitiationService, InitiateExitCommand
from exit_workflow.services.inspection import InspectionReportCommand, InspectionService
from exit_workflow.services.settlement import SettlementService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exit-workflows", tags=["exit-workflows"])

WorkflowId = Path(description="Workflow ID in the form EX-YYYYMMDD-NNNNN (rules.yaml#EXIT-02).")

_ERROR_RESPONSE = {"model": ErrorResponse}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=InitiateExitResponse,
    summary="Initiate an exit workflow",
    responses={
        409: {**_ERROR_RESPONSE, "description": "EXIT_ALREADY_IN_PROGRESS"},
        422: {
            **_ERROR_RESPONSE,
            "description": "MOVE_OUT_DATE_IN_PAST | REASON_INVALID | DOCUMENTS_REQUIRED",
        },
        501: {**_ERROR_RESPONSE, "description": "Blocked on an unresolved specification item"},
    },
)
async def initiate_exit(
    body: InitiateExitRequest,
    session: SessionDep,
    principal: PrincipalDep,
    reasons: ReasonsDep,
    settings: SettingsDep,
    clock: ClockDep,
    dispatcher: DispatcherDep,
) -> InitiateExitResponse:
    """algorithm.md steps 1 to 5. api.yaml authz: tenant, own active contract only."""
    service = ExitInitiationService(session, reasons=reasons, settings=settings, clock=clock)
    result = await service.initiate(
        InitiateExitCommand(
            contract_id=body.contract_id,
            move_out_date=body.move_out_date,
            reason=body.reason,
            documents=tuple(body.documents),
        ),
        principal,
    )

    # algorithm.md step 4 ends here: the workflow, the exit lock and the audit
    # rows are now durable.
    await session.commit()

    # algorithm.md step 5 / rules.yaml#EXIT-04 — AFTER COMMIT. A failure here
    # leaves the workflow exactly as committed and the event queued for retry;
    # it never propagates to the caller, who has a valid workflow either way
    # (edges.yaml#X-002).
    try:
        await dispatcher.dispatch_event(result.notification_event_id)
    except Exception:  # noqa: BLE001 - initiation must not fail on notification
        logger.exception(
            "post-commit dispatch failed for workflow %s; event %s remains queued",
            result.workflow_id,
            result.notification_event_id,
        )

    # api.yaml declares this body as {workflow_id, status: INITIATED}; the
    # persisted state is DOCS_SUBMITTED (blockers.md#B-8).
    return InitiateExitResponse(workflow_id=result.workflow_id, status=str(State.INITIATED))


@router.post(
    "/{workflow_id}/schedule-inspection",
    response_model=WorkflowStateResponse,
    summary="Owner schedules the move-out inspection",
    responses={409: {**_ERROR_RESPONSE, "description": "WRONG_STATE"}},
)
async def schedule_inspection(
    session: SessionDep,
    principal: PrincipalDep,
    clock: ClockDep,
    workflow_id: str = WorkflowId,
) -> WorkflowStateResponse:
    """algorithm.md step 6. api.yaml authz: owner."""
    workflow = await InspectionService(session, clock=clock).schedule_inspection(
        workflow_id, principal
    )
    await session.commit()
    return WorkflowStateResponse(workflow_id=workflow.id, status=workflow.status)


@router.post(
    "/{workflow_id}/inspection-report",
    response_model=WorkflowStateResponse,
    summary="Inspection agency submits the damage assessment",
    responses={409: {**_ERROR_RESPONSE, "description": "WRONG_STATE"}},
)
async def submit_inspection_report(
    body: InspectionReportRequest,
    session: SessionDep,
    principal: PrincipalDep,
    clock: ClockDep,
    workflow_id: str = WorkflowId,
) -> WorkflowStateResponse:
    """algorithm.md step 7. api.yaml authz: inspection_agency."""
    workflow = await InspectionService(session, clock=clock).submit_report(
        workflow_id,
        InspectionReportCommand(damage_amount=body.damage_amount, photos=tuple(body.photos)),
        principal,
    )
    await session.commit()
    return WorkflowStateResponse(workflow_id=workflow.id, status=workflow.status)


@router.post(
    "/{workflow_id}/confirm-damage",
    response_model=WorkflowStateResponse,
    summary="Owner confirms the damage assessment",
    responses={409: {**_ERROR_RESPONSE, "description": "WRONG_STATE"}},
)
async def confirm_damage(
    session: SessionDep,
    principal: PrincipalDep,
    clock: ClockDep,
    workflow_id: str = WorkflowId,
) -> WorkflowStateResponse:
    """algorithm.md step 8. api.yaml authz: owner."""
    workflow = await DamageService(session, clock=clock).confirm_damage(workflow_id, principal)
    await session.commit()
    return WorkflowStateResponse(workflow_id=workflow.id, status=workflow.status)


@router.post(
    "/{workflow_id}/settle",
    response_model=SettlementResponse,
    summary="Settle the deposit and issue the NOC",
    responses={
        409: {**_ERROR_RESPONSE, "description": "WRONG_STATE | PAYMENT_PENDING"},
        501: {**_ERROR_RESPONSE, "description": "SPEC_UNRESOLVED_R8 — damage exceeds deposit"},
    },
)
async def settle(
    session: SessionDep,
    principal: PrincipalDep,
    gateway: GatewayDep,
    noc_storage: NocStorageDep,
    clock: ClockDep,
    workflow_id: str = WorkflowId,
) -> SettlementResponse:
    """algorithm.md steps 9 to 13. api.yaml authz: system|owner.

    Steps 12 and 13 share this request's transaction, so the NOC record, the
    COMPLETE status and the released exit lock commit together
    (rules.yaml#EXIT-09).
    """
    service = SettlementService(session, gateway=gateway, noc_storage=noc_storage, clock=clock)
    try:
        result = await service.settle(workflow_id, principal)
    except ExitWorkflowError as exc:
        # A hold is not a failure: the refund may already be with the gateway,
        # so the payment record is committed before the caller is told to wait
        # (algorithm.md step 11). Everything else rolls back.
        if exc.preserves_transaction:
            await session.commit()
        raise
    await session.commit()
    return SettlementResponse(
        refund_amount=result.refund_amount,
        payment_id=result.payment_id,
        status=str(result.status),
    )
