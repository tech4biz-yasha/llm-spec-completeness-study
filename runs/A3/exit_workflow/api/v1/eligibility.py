"""BR-1 — contract creation eligibility."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from exit_workflow.api.deps import EligibilityDep, PrincipalDep
from exit_workflow.api.schemas.eligibility import (
    ContractEligibilityResponse,
    EligibilityBlockResponse,
)
from exit_workflow.core.errors import ValidationError

router = APIRouter(tags=["eligibility"])


@router.get(
    "/contract-eligibility",
    response_model=ContractEligibilityResponse,
    summary="May a new contract be created? (BR-1)",
    description=(
        "Called by the contract-creation flow before writing a new contract. When "
        "`allowed` is false the caller must block the action and display "
        "`warning_messages`."
    ),
)
async def check_contract_eligibility(
    eligibility: EligibilityDep,
    principal: PrincipalDep,
    property_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> ContractEligibilityResponse:
    if property_id is None and tenant_id is None:
        raise ValidationError("Provide property_id, tenant_id, or both.")
    result = await eligibility.check_contract_creation(
        property_id=property_id, tenant_id=tenant_id
    )
    return ContractEligibilityResponse(
        allowed=result.allowed,
        blocks=[EligibilityBlockResponse(**block.__dict__) for block in result.blocks],
        warning_messages=result.warning_messages,
    )
