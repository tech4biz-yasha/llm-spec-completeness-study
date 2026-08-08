"""Admin tasks.

rules.yaml#EXIT-05 requires "an admin task is created" when a workflow stalls, and
#EXIT-04 requires a "dead-letter + admin alert" when owner notification exhausts its
retries. The kit defines no admin task schema, assignment or SLA, so the row carries only
the type, the workflow and the facts that produced it. See blockers.md#B-11.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..clock import UTC
from ..db.models import AdminTask
from ..enums import AdminTaskStatus, AdminTaskType
from .ids import new_admin_task_id


def open_admin_task(
    session: Session,
    *,
    task_type: AdminTaskType,
    workflow_id: str,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> str:
    """Open a task, idempotently: one open task per (workflow, type).

    The stall sweep and the outbox dispatcher both run repeatedly; neither should pile up
    duplicate tasks for the same condition.
    """
    statement = (
        insert(AdminTask)
        .values(
            id=new_admin_task_id(),
            type=str(task_type),
            workflow_id=workflow_id,
            status=str(AdminTaskStatus.OPEN),
            payload=payload,
            created_at=occurred_at.astimezone(UTC),
        )
        .on_conflict_do_nothing(index_elements=["workflow_id", "type"])
        .returning(AdminTask.id)
    )
    created = session.execute(statement).scalar_one_or_none()
    if created is not None:
        return created
    existing = session.execute(
        AdminTask.__table__.select().where(
            (AdminTask.workflow_id == workflow_id) & (AdminTask.type == str(task_type))
        )
    ).one()
    return existing.id
