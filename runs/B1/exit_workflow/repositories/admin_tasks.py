"""Admin task access (rules.yaml#EXIT-04, #EXIT-05)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import AdminTask
from exit_workflow.domain.enums import AdminTaskStatus, AdminTaskType


class AdminTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def open_task(
        self,
        *,
        task_type: AdminTaskType,
        workflow_id: str | None,
        payload: dict[str, Any] | None = None,
    ) -> AdminTask:
        task = AdminTask(
            id=uuid.uuid4(),
            task_type=str(task_type),
            workflow_id=workflow_id,
            status=str(AdminTaskStatus.OPEN),
            payload=payload or {},
        )
        self._session.add(task)
        return task

    async def open_tasks_for(self, workflow_id: str) -> list[AdminTask]:
        result = await self._session.scalars(
            select(AdminTask).where(
                AdminTask.workflow_id == workflow_id,
                AdminTask.status == str(AdminTaskStatus.OPEN),
            )
        )
        return list(result)
