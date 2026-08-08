"""BR-1 contract-eligibility endpoint (SRS §4.7)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import ServicesDep, require_roles
from app.core.context import RequestContext
from app.domain.enums import ActorRole
from app.schemas.contract_guard import ContractEligibilityResponse

router = APIRouter(prefix="/contract-eligibility", tags=["business-rules"])

CallerCtx = Annotated[
    RequestContext,
    Depends(require_roles(ActorRole.OWNER, ActorRole.TENANT, ActorRole.ADMIN)),
]


@router.get(
    "",
    response_model=ContractEligibilityResponse,
    summary="Check BR-1 before creating a contract",
    description=(
        "SRS BR-1: an owner cannot create a new contract for a property, and a tenant "
        "cannot enter a new contract, while an exit workflow for them is still in "
        "progress. Returns `allowed` plus the warning text to display when blocked.\n\n"
        "This is the advisory read. The contract-creation path must additionally call "
        "the guard inside its own insert transaction -- a read here followed by an "
        "insert later is racy on its own."
    ),
)
async def check_contract_eligibility(
    services: ServicesDep,
    ctx: CallerCtx,
    property_id: Annotated[uuid.UUID | None, Query()] = None,
    tenant_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ContractEligibilityResponse:
    return await services.guard.check(property_id=property_id, tenant_id=tenant_id)
