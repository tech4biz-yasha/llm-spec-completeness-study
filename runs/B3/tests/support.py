"""Helpers that drive a workflow to a given state through the real service methods."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from exit_workflow.db.models import ExitWorkflow, ExitWorkflowAudit
from exit_workflow.enums import WorkflowState
from exit_workflow.services.identity import Principal
from exit_workflow.services.workflow import ExitWorkflowService

from .conftest import CONTRACT_ID


def initiate(
    service: ExitWorkflowService,
    tenant: Principal,
    move_out_date: date,
    *,
    contract_id: str = CONTRACT_ID,
    reason: str = "END_OF_TENANCY",
    documents: list[Any] | None = None,
):
    return service.initiate(
        principal=tenant,
        contract_id=contract_id,
        move_out_date=move_out_date,
        reason=reason,
        documents=documents if documents is not None else [{"type": "EJARI", "id": "DOC-1"}],
    )


def drive_to_damage_confirmed(
    service: ExitWorkflowService,
    *,
    tenant: Principal,
    owner: Principal,
    agency: Principal,
    move_out_date: date,
    damage_amount: Decimal,
) -> str:
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)
    service.schedule_inspection(result.workflow_id, principal=owner)
    service.submit_inspection_report(
        result.workflow_id,
        principal=agency,
        damage_amount=damage_amount,
        photos=["s3://photos/1.jpg"],
    )
    service.confirm_damage(result.workflow_id, principal=owner)
    return result.workflow_id


def load(session_factory: sessionmaker[Session], workflow_id: str) -> ExitWorkflow:
    with session_factory() as session:
        return session.execute(
            select(ExitWorkflow).where(ExitWorkflow.id == workflow_id)
        ).scalar_one()


def status(session_factory: sessionmaker[Session], workflow_id: str) -> WorkflowState:
    return load(session_factory, workflow_id).state


def audit_trail(
    session_factory: sessionmaker[Session], workflow_id: str
) -> list[ExitWorkflowAudit]:
    with session_factory() as session:
        return list(
            session.execute(
                select(ExitWorkflowAudit)
                .where(ExitWorkflowAudit.workflow_id == workflow_id)
                .order_by(ExitWorkflowAudit.id)
            )
            .scalars()
            .all()
        )
