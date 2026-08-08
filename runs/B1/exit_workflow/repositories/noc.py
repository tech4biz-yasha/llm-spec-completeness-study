"""NOC document access (rules.yaml#EXIT-09)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import NocDocument


class NocRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_workflow(self, workflow_id: str) -> NocDocument | None:
        return await self._session.scalar(
            select(NocDocument).where(NocDocument.workflow_id == workflow_id)
        )

    def record(
        self,
        *,
        workflow_id: str,
        bucket: str,
        region: str,
        object_key: str,
        content_sha256: str,
        byte_size: int,
    ) -> NocDocument:
        """Record an issued NOC.

        The row is immutable once written (DB trigger), matching
        rules.yaml#EXIT-09 "immutable once issued".
        """
        document = NocDocument(
            id=uuid.uuid4(),
            workflow_id=workflow_id,
            bucket=bucket,
            region=region,
            object_key=object_key,
            content_sha256=content_sha256,
            byte_size=byte_size,
        )
        self._session.add(document)
        return document
