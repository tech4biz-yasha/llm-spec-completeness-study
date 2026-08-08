"""BR-1 — the exit workflow contract lock.

    "Owner cannot create a new contract for a Property ID until the exit
     workflow for that property is marked COMPLETE. Tenant cannot enter into
     any new contract until their current exit workflow is fully completed.
     System must display appropriate warning messages and block the action if
     attempted."

Two layers enforce this:

1. This service, which other services (and the contract-creation flow) call to
   get a machine-readable allow/deny plus the warning text to display.
2. Partial unique indexes on ``exit_workflow`` which make a second live
   workflow for the same property, tenant or contract physically impossible —
   so two concurrent requests cannot both pass the check above.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.domain.enums import ACTIVE_STATUSES
from exit_workflow.models.workflow import ExitWorkflow

RULE_ID = "BR-1"


@dataclass(frozen=True, slots=True)
class EligibilityBlock:
    rule: str
    subject: str  # "PROPERTY" | "TENANT"
    subject_id: uuid.UUID
    workflow_id: uuid.UUID
    workflow_reference: str
    workflow_status: str
    message: str


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    allowed: bool
    blocks: list[EligibilityBlock] = field(default_factory=list)

    @property
    def warning_messages(self) -> list[str]:
        return [b.message for b in self.blocks]


def _property_message(wf: ExitWorkflow) -> str:
    return (
        f"A new contract cannot be created for this property: exit workflow "
        f"{wf.reference} is still in progress (status {wf.status.value}). The exit "
        f"workflow must be marked COMPLETE first."
    )


def _tenant_message(wf: ExitWorkflow) -> str:
    return (
        f"This tenant cannot enter into a new contract: their exit workflow "
        f"{wf.reference} is not yet complete (status {wf.status.value})."
    )


class EligibilityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_workflow_for_property(self, property_id: uuid.UUID) -> ExitWorkflow | None:
        stmt = select(ExitWorkflow).where(
            ExitWorkflow.property_id == property_id,
            ExitWorkflow.status.in_(ACTIVE_STATUSES),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def active_workflow_for_tenant(self, tenant_id: uuid.UUID) -> ExitWorkflow | None:
        stmt = select(ExitWorkflow).where(
            ExitWorkflow.tenant_id == tenant_id,
            ExitWorkflow.status.in_(ACTIVE_STATUSES),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def check_contract_creation(
        self,
        *,
        property_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
    ) -> EligibilityResult:
        blocks: list[EligibilityBlock] = []

        if property_id is not None:
            wf = await self.active_workflow_for_property(property_id)
            if wf is not None:
                blocks.append(
                    EligibilityBlock(
                        rule=RULE_ID,
                        subject="PROPERTY",
                        subject_id=property_id,
                        workflow_id=wf.id,
                        workflow_reference=wf.reference,
                        workflow_status=wf.status.value,
                        message=_property_message(wf),
                    )
                )

        if tenant_id is not None:
            wf = await self.active_workflow_for_tenant(tenant_id)
            if wf is not None:
                blocks.append(
                    EligibilityBlock(
                        rule=RULE_ID,
                        subject="TENANT",
                        subject_id=tenant_id,
                        workflow_id=wf.id,
                        workflow_reference=wf.reference,
                        workflow_status=wf.status.value,
                        message=_tenant_message(wf),
                    )
                )

        return EligibilityResult(allowed=not blocks, blocks=blocks)
