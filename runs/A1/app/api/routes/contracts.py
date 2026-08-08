"""Contract endpoints — the enforcement surface for BR-1."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ContextDep, SessionDep, SettingsDep
from app.schemas.settlement import BlockerOut, ContractOut, CreateContractRequest, EligibilityOut
from app.services.contract_service import ContractService

router = APIRouter(prefix="/contracts", tags=["contracts"])


@router.get(
    "/eligibility",
    response_model=EligibilityOut,
    summary="Check whether a new contract may be created (BR-1)",
)
async def check_eligibility(
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
    property_id: Annotated[uuid.UUID | None, Query()] = None,
    tenant_id: Annotated[uuid.UUID | None, Query()] = None,
) -> EligibilityOut:
    """Read-only probe so portals can warn *before* the user attempts the action."""
    service = ContractService(session, ctx, settings)
    result = await service.check_eligibility(property_id=property_id, tenant_id=tenant_id)
    return EligibilityOut(
        allowed=result.allowed,
        blockers=[BlockerOut(**b.as_dict()) for b in result.blockers],
        warnings=result.warning_messages,
    )


@router.post(
    "",
    response_model=ContractOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tenancy contract (blocked by an in-flight exit workflow)",
)
async def create_contract(
    payload: CreateContractRequest,
    session: SessionDep,
    ctx: ContextDep,
    settings: SettingsDep,
) -> ContractOut:
    service = ContractService(session, ctx, settings)
    contract = await service.create_contract(
        contract_number=payload.contract_number,
        property_id=payload.property_id,
        tenant_id=payload.tenant_id,
        start_date=payload.start_date,
        end_date=payload.end_date,
        security_deposit_fils=payload.security_deposit_fils,
        annual_rent_fils=payload.annual_rent_fils,
    )
    return ContractOut.model_validate(contract)
