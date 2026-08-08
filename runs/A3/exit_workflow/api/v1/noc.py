"""T13 steps 9-10 — Exit NOC retrieval, download and verification."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from exit_workflow.api.deps import NocServiceDep, WorkflowServiceDep
from exit_workflow.api.schemas.noc import NocResponse, NocVerificationResponse, RevokeNocRequest
from exit_workflow.api.v1.common import WORKFLOW_PATH_DESCRIPTION, workflow_identifier

router = APIRouter(tags=["noc"])

WorkflowRef = Annotated[str, Path(description=WORKFLOW_PATH_DESCRIPTION)]


@router.get(
    "/exit-workflows/{workflow_ref}/noc",
    response_model=NocResponse,
    summary="Exit NOC metadata",
)
async def get_noc(
    workflow_ref: WorkflowRef, workflows: WorkflowServiceDep, noc_service: NocServiceDep
) -> NocResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref))
    return NocResponse.model_validate(await noc_service.get(workflow))


@router.get(
    "/exit-workflows/{workflow_ref}/noc/download",
    response_class=Response,
    summary="Download the Exit NOC PDF (T13 step 9)",
    responses={200: {"content": {"application/pdf": {}}}},
)
async def download_noc(
    workflow_ref: WorkflowRef, workflows: WorkflowServiceDep, noc_service: NocServiceDep
) -> Response:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    noc, data = await noc_service.download(workflow)
    return Response(
        content=data,
        media_type=noc.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{noc.noc_number}.pdf"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Content-Length": str(len(data)),
            "X-Content-SHA256": noc.content_sha256,
        },
    )


@router.post(
    "/exit-workflows/{workflow_ref}/noc/revoke",
    response_model=NocResponse,
    summary="Administratively revoke an Exit NOC",
)
async def revoke_noc(
    workflow_ref: WorkflowRef,
    payload: RevokeNocRequest,
    workflows: WorkflowServiceDep,
    noc_service: NocServiceDep,
) -> NocResponse:
    workflow = await workflows.get(workflow_identifier(workflow_ref), for_update=True)
    return NocResponse.model_validate(await noc_service.revoke(workflow, reason=payload.reason))


@router.get(
    "/noc/verify",
    response_model=NocVerificationResponse,
    summary="Verify a presented Exit NOC",
    description=(
        "Confirms that a certificate number and its verification code belong to a "
        "genuine, unrevoked NOC. Returns only what a verifier needs."
    ),
)
async def verify_noc(
    noc_service: NocServiceDep,
    noc_number: Annotated[str, Query(max_length=24)],
    code: Annotated[str, Query(max_length=32)],
) -> NocVerificationResponse:
    result = await noc_service.verify(noc_number, code)
    return NocVerificationResponse(**result.__dict__)
