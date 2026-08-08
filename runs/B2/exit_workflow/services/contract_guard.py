"""The outward-facing half of the exit lock — edges.yaml#X-006, rules.yaml#EXIT-03.

    X-006: a new contract attempted on the property mid-exit -> 409
           EXIT_WORKFLOW_INCOMPLETE.

The contract module calls this before creating a contract. It lives here because
this module owns the lock.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import ExitWorkflow, Property
from ..domain.states import State
from ..errors import ExitWorkflowIncomplete, SpecUnresolved


async def assert_property_contractable(session: AsyncSession, property_id: uuid.UUID) -> None:
    """Raise EXIT_WORKFLOW_INCOMPLETE if the property is under an exit lock.

    rules.yaml#EXIT-03 — the lock "blocks new contracts on the property (BR-1)
    and is released only by workflow COMPLETE". Checked against the lock flag,
    with the open workflow id reported for the caller's error payload.
    """
    locked = (
        await session.execute(select(Property.exit_lock).where(Property.id == property_id))
    ).scalar_one_or_none()
    if not locked:
        return

    open_workflow = (
        await session.execute(
            select(ExitWorkflow.id)
            .where(ExitWorkflow.property_id == property_id)
            .where(ExitWorkflow.status != State.COMPLETE)
            .order_by(ExitWorkflow.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    raise ExitWorkflowIncomplete(
        "the property has an incomplete exit workflow and cannot take a new contract",
        details={"property_id": str(property_id), "workflow_id": open_workflow},
    )


async def assert_tenant_contractable(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """BR-1 read as a lock on the *tenant*, not the property.

    BLOCKED — risks.md#R1: "is the BR-1 lock scoped to the role, or to the
    identity?" A dual-role user exiting a tenancy may or may not be barred from
    contracting as an owner. Deciding it here would either freeze legitimate
    owner revenue or leave the absconding-tenant hole open, which is precisely
    the decision R1 reserves for the client.
    """
    raise SpecUnresolved(
        "R1",
        "tenant-scoped BR-1 lock is unresolved (risks.md#R1: role-scoped vs identity-scoped)",
        details={"tenant_id": str(tenant_id)},
    )
