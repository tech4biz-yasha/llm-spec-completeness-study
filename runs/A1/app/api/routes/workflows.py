"""Exit workflow endpoints (T13 steps 1-10, O16 settlement, BR-1 lock release)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response, status

from app.api.deps import ContextDep, SessionDep, SettingsDep
from app.schemas.inspection import (
    AssignmentOut,
    DamageReportOut,
    ReinspectionRequest,
    RequestInspectionRequest,
    SelectSlotRequest,
)
from app.schemas.settlement import NOCOut, PayRequest, SettlementOut
from app.schemas.workflow import (
    ApproveExitRequest,
    DocumentOut,
    DocumentUploadRequest,
    ExitWorkflowOut,
    ExitWorkflowSummaryOut,
    InitiateExitRequest,
    ReasonRequest,
    WorkflowListOut,
)
from app.services.inspection_service import InspectionService
from app.services.noc_service import NOCService
from app.services.settlement_service import SettlementService
from app.services.workflow_service import DocumentUpload, WorkflowService

router = APIRouter(prefix="/exit-workflows", tags=["exit-workflows"])


def _workflows(session, ctx, settings) -> WorkflowService:
    return WorkflowService(session, ctx, settings)


async def _detail(service: WorkflowService, workflow) -> ExitWorkflowOut:
    return ExitWorkflowOut.of(
        workflow, missing_documents=service.missing_required_documents(workflow)
    )


# --- T13 steps 1-5: initiation -------------------------------------------------------------


@router.post(
    "",
    response_model=ExitWorkflowOut,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate an exit workflow (T13 steps 1-3, 5)",
)
async def initiate_exit(
    payload: InitiateExitRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> ExitWorkflowOut:
    service = _workflows(session, ctx, settings)
    workflow = await service.initiate(
        contract_id=payload.contract_id,
        move_out_date=payload.move_out_date,
        reason_code=payload.reason_code,
        reason_text=payload.reason_text,
    )
    return await _detail(service, workflow)


@router.get("", response_model=WorkflowListOut, summary="List exit workflows for the caller")
async def list_workflows(
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
    active_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WorkflowListOut:
    service = _workflows(session, ctx, settings)
    items = await service.list_for_principal(active_only=active_only, limit=limit, offset=offset)
    return WorkflowListOut(
        items=[ExitWorkflowSummaryOut.of(w) for w in items], count=len(items)
    )


@router.get("/{workflow_id}", response_model=ExitWorkflowOut, summary="Fetch one exit workflow")
async def get_workflow(
    workflow_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> ExitWorkflowOut:
    service = _workflows(session, ctx, settings)
    workflow = await service.get(workflow_id)
    return await _detail(service, workflow)


# --- T13 step 4: documents ------------------------------------------------------------------


@router.post(
    "/{workflow_id}/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a document (T13 step 4)",
)
async def upload_document(
    workflow_id: uuid.UUID,
    payload: DocumentUploadRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> DocumentOut:
    service = _workflows(session, ctx, settings)
    document = await service.upload_document(
        workflow_id,
        DocumentUpload(
            kind=payload.kind,
            file_name=payload.file_name,
            content_type=payload.content_type,
            byte_size=payload.byte_size,
            storage_key=payload.storage_key,
            checksum_sha256=payload.checksum_sha256,
        ),
    )
    return DocumentOut.of(document)


# --- T13 step 6: submission and owner decision ------------------------------------------------


@router.post(
    "/{workflow_id}/submit",
    response_model=ExitWorkflowOut,
    summary="Submit to the owner (T13 step 6)",
)
async def submit_workflow(
    workflow_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> ExitWorkflowOut:
    service = _workflows(session, ctx, settings)
    workflow = await service.submit(workflow_id)
    return await _detail(service, workflow)


@router.post(
    "/{workflow_id}/approve",
    response_model=ExitWorkflowOut,
    summary="Owner approves the exit, optionally assigning an inspection agency (O15)",
)
async def approve_workflow(
    workflow_id: uuid.UUID,
    payload: ApproveExitRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> ExitWorkflowOut:
    service = _workflows(session, ctx, settings)
    workflow = await service.owner_approve(
        workflow_id, agency_id=payload.agency_id, instructions=payload.instructions
    )
    return await _detail(service, workflow)


@router.post(
    "/{workflow_id}/reject", response_model=ExitWorkflowOut, summary="Owner declines the exit"
)
async def reject_workflow(
    workflow_id: uuid.UUID,
    payload: ReasonRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> ExitWorkflowOut:
    service = _workflows(session, ctx, settings)
    workflow = await service.owner_reject(workflow_id, reason=payload.reason)
    return await _detail(service, workflow)


@router.post(
    "/{workflow_id}/cancel", response_model=ExitWorkflowOut, summary="Cancel an in-flight workflow"
)
async def cancel_workflow(
    workflow_id: uuid.UUID,
    payload: ReasonRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> ExitWorkflowOut:
    service = _workflows(session, ctx, settings)
    workflow = await service.cancel(workflow_id, reason=payload.reason)
    return await _detail(service, workflow)


# --- T13 step 7: inspection scheduling ---------------------------------------------------------


@router.post(
    "/{workflow_id}/inspection",
    response_model=AssignmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Request an inspection from a registered agency (O15)",
)
async def request_inspection(
    workflow_id: uuid.UUID,
    payload: RequestInspectionRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> AssignmentOut:
    service = InspectionService(session, ctx, settings)
    assignment = await service.request_inspection_by_id(
        workflow_id, agency_id=payload.agency_id, instructions=payload.instructions
    )
    return AssignmentOut.of(assignment)


@router.post(
    "/{workflow_id}/inspection/select-slot",
    response_model=AssignmentOut,
    summary="Owner or tenant selects an appointment window (O15)",
)
async def select_slot(
    workflow_id: uuid.UUID,
    payload: SelectSlotRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> AssignmentOut:
    service = InspectionService(session, ctx, settings)
    assignment = await service.select_slot(workflow_id, payload.slot_id)
    return AssignmentOut.of(assignment)


@router.post(
    "/{workflow_id}/inspection/reinspect",
    response_model=AssignmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Order a re-inspection after a disputed damage report",
)
async def order_reinspection(
    workflow_id: uuid.UUID,
    payload: ReinspectionRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> AssignmentOut:
    service = InspectionService(session, ctx, settings)
    assignment = await service.order_reinspection(
        workflow_id, agency_id=payload.agency_id, reason=payload.reason
    )
    return AssignmentOut.of(assignment)


@router.get(
    "/{workflow_id}/damage-report",
    response_model=DamageReportOut,
    summary="Fetch the damage report under review (T13 step 8)",
)
async def get_damage_report(
    workflow_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> DamageReportOut:
    from app.errors import NotFoundError

    service = _workflows(session, ctx, settings)
    workflow = await service.get(workflow_id)
    assignment = workflow.current_assignment
    if assignment is None or assignment.report is None:
        raise NotFoundError(
            "no damage report has been submitted for this workflow",
            details={"workflow_id": str(workflow_id)},
        )
    await session.refresh(assignment.report, ["line_items"])
    return DamageReportOut.of(assignment.report)


# --- T13 step 9: settlement ---------------------------------------------------------------------


@router.get(
    "/{workflow_id}/settlement",
    response_model=SettlementOut,
    summary="Fetch the deposit settlement (O16)",
)
async def get_settlement(
    workflow_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> SettlementOut:
    service = SettlementService(session, ctx, settings)
    return SettlementOut.of(await service.get(workflow_id))


@router.post(
    "/{workflow_id}/settlement/approve",
    response_model=SettlementOut,
    summary="Owner approves the deductions, making the settlement payable",
)
async def approve_settlement(
    workflow_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> SettlementOut:
    service = SettlementService(session, ctx, settings)
    return SettlementOut.of(await service.approve(workflow_id))


@router.post(
    "/{workflow_id}/settlement/dispute",
    response_model=ExitWorkflowOut,
    summary="Tenant disputes the settlement, returning it to damage review",
)
async def dispute_settlement(
    workflow_id: uuid.UUID,
    payload: ReasonRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> ExitWorkflowOut:
    service = SettlementService(session, ctx, settings)
    workflow = await service.dispute(workflow_id, reason=payload.reason)
    return await _detail(_workflows(session, ctx, settings), workflow)


@router.post(
    "/{workflow_id}/settlement/pay",
    response_model=SettlementOut,
    summary="Pay Deposit — settles a leg and auto-issues the NOC when the last one clears",
)
async def pay_settlement(
    workflow_id: uuid.UUID,
    payload: PayRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SettlementOut:
    from app.errors import ValidationError

    key = payload.idempotency_key or idempotency_key
    if not key:
        raise ValidationError(
            "an idempotency key is required",
            details={"supply_via": ["Idempotency-Key header", "idempotency_key body field"]},
        )
    service = SettlementService(session, ctx, settings)
    await service.pay(workflow_id, leg=payload.leg, idempotency_key=key)
    return SettlementOut.of(await service.get(workflow_id))


# --- T13 step 10: NOC and completion ---------------------------------------------------------------


@router.get("/{workflow_id}/noc", response_model=NOCOut, summary="Exit NOC metadata")
async def get_noc(
    workflow_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> NOCOut:
    service = NOCService(session, ctx, settings)
    noc = await service.get(workflow_id)
    return NOCOut.of(noc, download_url=str(request.url_for("download_noc", workflow_id=workflow_id)))


@router.get(
    "/{workflow_id}/noc/download",
    name="download_noc",
    response_class=Response,
    summary="Download the digital Exit NOC and complete the workflow (T13 step 10)",
    responses={200: {"content": {"application/pdf": {}}, "description": "The signed certificate"}},
)
async def download_noc(
    workflow_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> Response:
    service = NOCService(session, ctx, settings)
    noc = await service.download(workflow_id)
    return Response(
        content=noc.pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{noc.noc_number}.pdf"',
            "Content-Length": str(noc.byte_size),
            "X-Content-SHA256": noc.content_sha256,
            "Cache-Control": "private, no-store",
        },
    )


@router.post(
    "/{workflow_id}/complete",
    response_model=ExitWorkflowOut,
    summary="Close the workflow explicitly, releasing the BR-1 lock",
)
async def complete_workflow(
    workflow_id: uuid.UUID, session: SessionDep, ctx: ContextDep, settings: SettingsDep
) -> ExitWorkflowOut:
    service = _workflows(session, ctx, settings)
    workflow = await service.complete_by_id(workflow_id)
    return await _detail(service, workflow)
