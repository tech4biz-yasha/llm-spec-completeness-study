"""Guards this module exposes to the rest of the platform.

rules.yaml#EXIT-03: the exit lock "blocks new contracts on the property (BR-1)
and is released only by workflow COMPLETE", and edges.yaml#X-006 fixes the
response: 409 EXIT_WORKFLOW_INCOMPLETE.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.domain.errors import ExitWorkflowIncomplete, SpecUnresolved
from exit_workflow.repositories.properties import PropertyRepository
from exit_workflow.repositories.workflows import WorkflowRepository


async def assert_property_contractable(session: AsyncSession, property_id: uuid.UUID) -> None:
    """Refuse a new contract on a property whose exit workflow is incomplete.

    edges.yaml#X-006: "New contract attempted on the property mid-exit ->
    409 EXIT_WORKFLOW_INCOMPLETE."

    Call this from the contract creation path.
    """
    prop = await PropertyRepository(session).get(property_id)
    if prop is None or not prop.exit_lock:
        return

    workflow = await WorkflowRepository(session).get_active_for_property(property_id)
    raise ExitWorkflowIncomplete(
        "This property has an exit workflow in progress.",
        details={
            "property_id": str(property_id),
            "workflow_id": prop.exit_lock_workflow_id or (workflow.id if workflow else None),
            "workflow_status": workflow.status if workflow else None,
        },
    )


async def assert_tenant_contractable(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """The identity-scoped half of BR-1 — not implementable.

    risks.md#R1 asks whether the BR-1 lock is scoped to the role or to the
    identity: a dual-role user under one Master Customer ID (BR-4) who is
    exiting a tenancy may or may not be barred from contracting as an *owner*.
    Both readings are live, and the wrong one either freezes legitimate owner
    revenue or leaves open the hole BR-1 exists to close.

    edges.yaml#X-006 only specifies the property-scoped guard, which
    :func:`assert_property_contractable` implements. This one refuses to answer.
    """
    raise SpecUnresolved(
        "R1",
        "Whether the BR-1 exit lock is scoped to the tenant role or to the identity is "
        "undecided (risks.md#R1). The tenant-scoped contract guard cannot be evaluated.",
        tenant_id=str(tenant_id),
    )
