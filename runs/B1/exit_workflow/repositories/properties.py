"""Property exit lock (rules.yaml#EXIT-03)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import Property


class PropertyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, property_id: uuid.UUID, *, for_update: bool = False) -> Property | None:
        statement = select(Property).where(Property.id == property_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def acquire_exit_lock(self, property_id: uuid.UUID, workflow_id: str) -> Property:
        """Set ``exit_lock`` true (rules.yaml#EXIT-03).

        Called inside the initiation transaction — never on its own — so the
        lock and the workflow row commit together or not at all.
        """
        prop = await self.get(property_id, for_update=True)
        if prop is None:
            raise LookupError(f"property {property_id} does not exist")
        prop.exit_lock = True
        prop.exit_lock_workflow_id = workflow_id
        return prop

    async def release_exit_lock(self, property_id: uuid.UUID, workflow_id: str) -> Property:
        """Release the lock (rules.yaml#EXIT-03, #EXIT-09: only by COMPLETE)."""
        prop = await self.get(property_id, for_update=True)
        if prop is None:
            raise LookupError(f"property {property_id} does not exist")
        prop.exit_lock = False
        prop.exit_lock_workflow_id = None
        return prop
