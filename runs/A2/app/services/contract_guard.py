"""BR-1 enforcement (SRS §4.7, Owner BRD 3.17).

    "Owner cannot create a new contract for a Property ID until the exit workflow for
     that property is marked COMPLETE. Tenant cannot enter into any new contract until
     their current exit workflow is fully completed. System must display appropriate
     warning messages and block the action if attempted."

Two enforcement points, deliberately:

* :meth:`ContractGuard.check` is the advisory read the Property service calls before it
  offers a "create contract" button -- it returns the warning text to display.
* :meth:`ContractGuard.assert_allowed` raises, and is what the contract-creation path
  must call. A read-then-act check is racy on its own; the caller is expected to invoke
  it inside the same transaction that inserts the contract.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailedError
from app.domain.policies import ContractBlock, assert_contract_creation_allowed
from app.models.exit_workflow import ExitWorkflow
from app.repositories.exit_workflow import ExitWorkflowRepository
from app.schemas.contract_guard import BlockingWorkflow, ContractEligibilityResponse


def _to_block(workflow: ExitWorkflow, scope: str) -> ContractBlock:
    return ContractBlock(
        workflow_id=str(workflow.id),
        reference=workflow.reference or str(workflow.id),
        state=workflow.state,
        property_id=str(workflow.property_id),
        tenant_id=str(workflow.tenant_id),
        scope=scope,
    )


class ContractGuard:
    def __init__(self, session: AsyncSession) -> None:
        self._repo = ExitWorkflowRepository(session)

    async def find_blocks(
        self,
        *,
        property_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> list[ContractBlock]:
        if property_id is None and tenant_id is None:
            raise ValidationFailedError(
                "Supply property_id, tenant_id, or both.",
                details={"fields": ["property_id", "tenant_id"]},
            )

        blocks: list[ContractBlock] = []
        seen: set[tuple[str, str]] = set()

        if property_id is not None:
            for workflow in await self._repo.find_blocking_for_property(property_id):
                block = _to_block(workflow, "PROPERTY")
                key = (block.workflow_id, block.scope)
                if key not in seen:
                    seen.add(key)
                    blocks.append(block)

        if tenant_id is not None:
            for workflow in await self._repo.find_blocking_for_tenant(tenant_id):
                block = _to_block(workflow, "TENANT")
                key = (block.workflow_id, block.scope)
                if key not in seen:
                    seen.add(key)
                    blocks.append(block)

        return blocks

    async def check(
        self,
        *,
        property_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> ContractEligibilityResponse:
        blocks = await self.find_blocks(property_id=property_id, tenant_id=tenant_id)
        return ContractEligibilityResponse(
            allowed=not blocks,
            property_id=property_id,
            tenant_id=tenant_id,
            blocking_workflows=[
                BlockingWorkflow(
                    workflow_id=uuid.UUID(b.workflow_id),
                    reference=b.reference,
                    state=b.state,
                    scope=b.scope,
                    property_id=uuid.UUID(b.property_id),
                    tenant_id=uuid.UUID(b.tenant_id),
                    message=b.message,
                )
                for b in blocks
            ],
            warnings=[b.message for b in blocks],
        )

    async def assert_allowed(
        self,
        *,
        property_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> None:
        """Raise :class:`BusinessRuleViolationError` (409) if BR-1 blocks the action."""
        assert_contract_creation_allowed(
            await self.find_blocks(property_id=property_id, tenant_id=tenant_id)
        )
