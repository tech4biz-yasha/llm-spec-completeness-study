"""Exit NOC endpoints (SRS T13 step 10, O16)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from app.api.deps import ContextDep, LockedWorkflowDep, ServicesDep, WorkflowDep, require_roles
from app.api.presenters import noc as present_noc
from app.api.rate_limit import public_rate_limit
from app.core.context import RequestContext
from app.domain.enums import ActorRole
from app.schemas.noc import NocResponse, NocVerificationResponse, RevokeNocRequest

router = APIRouter(tags=["noc"])

BASE_PATH = "/api/v1/exit-workflows"

AdminCtx = Annotated[RequestContext, Depends(require_roles(ActorRole.ADMIN))]


@router.get(
    "/exit-workflows/{workflow_id}/noc",
    response_model=NocResponse,
    summary="Fetch Exit NOC metadata",
)
async def get_noc(workflow: WorkflowDep, services: ServicesDep) -> NocResponse:
    return present_noc(await services.nocs.get(workflow), base_path=BASE_PATH)


@router.get(
    "/exit-workflows/{workflow_id}/noc/content",
    summary="Download the Exit NOC",
    description=(
        "SRS T13 step 10. Streams the PDF and records the download in the audit trail. "
        "The response carries `X-Checksum-SHA256`, which matches the value published "
        "in the NOC metadata."
    ),
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def download_noc(
    workflow: LockedWorkflowDep, services: ServicesDep, ctx: ContextDep
) -> Response:
    noc, data = await services.nocs.download(workflow, ctx)
    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{noc.noc_number}.pdf"',
            "Content-Length": str(len(data)),
            "X-Checksum-SHA256": noc.checksum_sha256,
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/noc/verify/{verification_code}",
    response_model=NocVerificationResponse,
    summary="Verify an Exit NOC (public)",
    description=(
        "Unauthenticated. Confirms that a certificate exists and is valid without "
        "disclosing the parties or any settlement figure. Rate limited."
    ),
    dependencies=[Depends(public_rate_limit)],
)
async def verify_noc(
    services: ServicesDep,
    response: Response,
    verification_code: Annotated[str, Path(min_length=4, max_length=32)],
) -> NocVerificationResponse:
    response.headers["Cache-Control"] = "no-store"
    return await services.nocs.verify(verification_code)


@router.post(
    "/exit-workflows/{workflow_id}/noc/revoke",
    response_model=NocResponse,
    summary="Revoke an Exit NOC issued in error",
)
async def revoke_noc(
    workflow: LockedWorkflowDep,
    payload: RevokeNocRequest,
    services: ServicesDep,
    ctx: AdminCtx,
) -> NocResponse:
    noc = await services.nocs.revoke(workflow, payload.reason, ctx)
    return present_noc(noc, base_path=BASE_PATH)
