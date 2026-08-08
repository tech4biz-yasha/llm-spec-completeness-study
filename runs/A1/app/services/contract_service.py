"""BR-1 — Exit Workflow Lock.

    "Owner cannot create a new contract for a Property ID until the exit workflow for that
     property is marked COMPLETE. Tenant cannot enter into any new contract until their
     current exit workflow is fully completed. System must display appropriate warning
     messages and block the action if attempted."

Two readings of "COMPLETE" are possible. This module locks on *in-flight* workflows: a
workflow that was cancelled or rejected never resulted in an exit, so continuing to block the
property or tenant would strand them permanently. Only the states in ``ACTIVE_STATES`` block.

The rule is enforced in three places, deliberately:

1. :meth:`ContractService.check_eligibility` — a read-only probe the portals call to render
   the warning message *before* the user attempts the action.
2. :meth:`ContractService.create_contract` — the blocking check on the write path.
3. Partial unique indexes on ``exit_workflows`` — the backstop that holds under concurrency,
   where two simultaneous requests could both pass step 2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

import sqlalchemy as sa

from app.errors import ContractBlockedError, NotFoundError, ValidationError
from app.models.audit import AuditAction
from app.models.catalog import Contract, ContractStatus, Property, Tenant
from app.models.workflow import ExitWorkflow
from app.security import PrincipalRole
from app.services.base import ServiceBase


class BlockScope(StrEnum):
    PROPERTY = "PROPERTY"
    TENANT = "TENANT"


@dataclass(frozen=True, slots=True)
class Blocker:
    scope: BlockScope
    workflow_id: uuid.UUID
    reference: str
    state: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "scope": self.scope.value,
            "workflow_id": str(self.workflow_id),
            "reference": self.reference,
            "state": self.state,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    allowed: bool
    blockers: list[Blocker] = field(default_factory=list)

    @property
    def warning_messages(self) -> list[str]:
        return [b.message for b in self.blockers]


_PROPERTY_MESSAGE = (
    "A new contract cannot be created for this property yet. Exit workflow {reference} is "
    "still in progress (currently {state}). The property is released once that workflow is "
    "complete."
)
_TENANT_MESSAGE = (
    "This tenant cannot enter into a new contract yet. Their exit workflow {reference} is "
    "still in progress (currently {state}) and must be completed first."
)


class ContractService(ServiceBase):
    async def check_eligibility(
        self,
        *,
        property_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> EligibilityResult:
        """Report whether a new contract may be created, with user-facing warnings."""
        if property_id is None and tenant_id is None:
            raise ValidationError("supply at least one of property_id or tenant_id")

        predicates = []
        if property_id is not None:
            predicates.append(ExitWorkflow.property_id == property_id)
        if tenant_id is not None:
            predicates.append(ExitWorkflow.tenant_id == tenant_id)

        rows = (
            await self.session.scalars(
                sa.select(ExitWorkflow).where(
                    ExitWorkflow.is_active.is_(True), sa.or_(*predicates)
                )
            )
        ).all()

        blockers: list[Blocker] = []
        for workflow in rows:
            if property_id is not None and workflow.property_id == property_id:
                blockers.append(
                    Blocker(
                        scope=BlockScope.PROPERTY,
                        workflow_id=workflow.id,
                        reference=workflow.reference,
                        state=workflow.state.value,
                        message=_PROPERTY_MESSAGE.format(
                            reference=workflow.reference, state=workflow.state.value
                        ),
                    )
                )
            if tenant_id is not None and workflow.tenant_id == tenant_id:
                blockers.append(
                    Blocker(
                        scope=BlockScope.TENANT,
                        workflow_id=workflow.id,
                        reference=workflow.reference,
                        state=workflow.state.value,
                        message=_TENANT_MESSAGE.format(
                            reference=workflow.reference, state=workflow.state.value
                        ),
                    )
                )

        return EligibilityResult(allowed=not blockers, blockers=blockers)

    async def assert_can_create_contract(
        self, *, property_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        result = await self.check_eligibility(property_id=property_id, tenant_id=tenant_id)
        if not result.allowed:
            # Written in its own transaction: the request that triggered it is about to be
            # rolled back, but the blocked attempt must still be on the record.
            await self.audit.record_out_of_band(
                AuditAction.CONTRACT_BLOCKED,
                entity_type="Property",
                entity_id=property_id,
                workflow_id=result.blockers[0].workflow_id,
                payload={
                    "tenant_id": str(tenant_id),
                    "blockers": [b.as_dict() for b in result.blockers],
                },
            )
            raise ContractBlockedError(
                "; ".join(result.warning_messages),
                details={"blockers": [b.as_dict() for b in result.blockers]},
            )

    async def create_contract(
        self,
        *,
        contract_number: str,
        property_id: uuid.UUID,
        tenant_id: uuid.UUID,
        start_date: date,
        end_date: date,
        security_deposit_fils: int,
        annual_rent_fils: int,
    ) -> Contract:
        """Create a tenancy contract, subject to BR-1."""
        self.ctx.require_role(PrincipalRole.OWNER, PrincipalRole.ADMIN)

        prop = await self.session.get(Property, property_id)
        if prop is None:
            raise NotFoundError("property not found", details={"property_id": str(property_id)})
        tenant = await self.session.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFoundError("tenant not found", details={"tenant_id": str(tenant_id)})

        principal = self.ctx.require_principal()
        if not principal.is_admin and prop.owner_id != principal.id:
            from app.errors import AuthorizationError

            raise AuthorizationError("caller does not own this property")
        if end_date <= start_date:
            raise ValidationError("end_date must be after start_date")
        if security_deposit_fils < 0 or annual_rent_fils < 0:
            raise ValidationError("monetary amounts cannot be negative")

        await self.assert_can_create_contract(property_id=property_id, tenant_id=tenant_id)

        contract = Contract(
            contract_number=contract_number,
            property_id=property_id,
            tenant_id=tenant_id,
            owner_id=prop.owner_id,
            status=ContractStatus.ACTIVE,
            start_date=start_date,
            end_date=end_date,
            security_deposit_fils=security_deposit_fils,
            annual_rent_fils=annual_rent_fils,
        )
        self.session.add(contract)
        await self.session.flush()

        self.audit.record(
            AuditAction.CONTRACT_CREATED,
            entity_type="Contract",
            entity_id=contract.id,
            payload={
                "contract_number": contract_number,
                "property_id": str(property_id),
                "tenant_id": str(tenant_id),
                "security_deposit_fils": security_deposit_fils,
            },
        )
        return contract
