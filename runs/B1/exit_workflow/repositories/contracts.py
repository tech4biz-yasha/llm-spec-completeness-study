"""Contract access (read-only from this module)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import Contract


class ContractRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, contract_id: uuid.UUID, *, for_update: bool = False) -> Contract | None:
        statement = select(Contract).where(Contract.id == contract_id)
        if for_update:
            # Held for the length of the initiation transaction so the contract
            # cannot be terminated between the ACTIVE check (rules.yaml#EXIT-01)
            # and the workflow insert.
            statement = statement.with_for_update()
        return await self._session.scalar(statement)
