"""The one place a workflow's status is allowed to change."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.domain.enums import ExitWorkflowStatus, TERMINAL_STATUSES
from exit_workflow.domain.state_machine import assert_can_transition
from exit_workflow.models.audit import WorkflowTransition
from exit_workflow.models.workflow import ExitWorkflow
from exit_workflow.services.audit import AuditRecorder
from exit_workflow.services.context import ServiceContext
from exit_workflow.services.events import AggregateType, EventRecorder, EventType

#: Terminal-status events, so the contract service can release the BR-1 lock.
_STATUS_EVENTS: dict[ExitWorkflowStatus, str] = {
    ExitWorkflowStatus.PENDING_OWNER_APPROVAL: EventType.WORKFLOW_SUBMITTED,
    ExitWorkflowStatus.OWNER_APPROVED: EventType.WORKFLOW_APPROVED,
    ExitWorkflowStatus.REJECTED: EventType.WORKFLOW_REJECTED,
    ExitWorkflowStatus.CANCELLED: EventType.WORKFLOW_CANCELLED,
    ExitWorkflowStatus.COMPLETED: EventType.WORKFLOW_COMPLETED,
}


def apply_transition(
    session: AsyncSession,
    ctx: ServiceContext,
    audit: AuditRecorder,
    events: EventRecorder,
    workflow: ExitWorkflow,
    target: ExitWorkflowStatus,
    *,
    reason: str | None = None,
    system: bool = False,
    attributes: dict[str, Any] | None = None,
) -> WorkflowTransition:
    """Validate, apply and record a status change.

    ``system=True`` marks an automatic transition (NOC issuance, completion)
    that no human requested; the topology is still enforced.
    """

    source = workflow.status
    assert_can_transition(source, target, None if system else ctx.role)

    now = utcnow()
    workflow.status = target

    if target is ExitWorkflowStatus.COMPLETED:
        workflow.completed_at = now
    if target in TERMINAL_STATUSES:
        workflow.closed_at = now
        workflow.closure_reason = reason
        workflow.closed_by = ctx.actor_id

    transition = WorkflowTransition(
        workflow_id=workflow.id,
        from_status=source,
        to_status=target,
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        reason=reason,
        occurred_at=now,
        attributes=attributes or {},
    )
    session.add(transition)

    audit.record(
        ctx,
        action=f"exit_workflow.transition.{target.value.lower()}",
        entity_type="exit_workflow",
        entity_id=workflow.id,
        workflow_id=workflow.id,
        changes={"from": source.value, "to": target.value, "reason": reason},
    )

    payload: dict[str, Any] = {
        "reference": workflow.reference,
        "from_status": source.value,
        "to_status": target.value,
        "property_id": workflow.property_id,
        "tenant_id": workflow.tenant_id,
        "owner_id": workflow.owner_id,
        "contract_id": workflow.contract_id,
        "reason": reason,
    }
    events.emit(
        ctx,
        event_type=EventType.WORKFLOW_STATUS_CHANGED,
        aggregate_type=AggregateType.WORKFLOW,
        aggregate_id=workflow.id,
        workflow_id=workflow.id,
        payload=payload,
    )
    if specific := _STATUS_EVENTS.get(target):
        events.emit(
            ctx,
            event_type=specific,
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id=workflow.id,
            workflow_id=workflow.id,
            payload=payload,
        )
    if target in TERMINAL_STATUSES:
        # BR-1: the property/tenant lock is released only here.
        events.emit(
            ctx,
            event_type=EventType.LOCK_RELEASED,
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id=workflow.id,
            workflow_id=workflow.id,
            payload={
                "reference": workflow.reference,
                "property_id": workflow.property_id,
                "tenant_id": workflow.tenant_id,
                "final_status": target.value,
                "completed": target is ExitWorkflowStatus.COMPLETED,
            },
        )
    return transition


def workflow_event_payload(workflow: ExitWorkflow, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "workflow_id": workflow.id,
        "reference": workflow.reference,
        "status": workflow.status.value,
        "property_id": workflow.property_id,
        "tenant_id": workflow.tenant_id,
        "owner_id": workflow.owner_id,
        "contract_id": workflow.contract_id,
    }
    payload.update(extra)
    return payload


def uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(value)
    except ValueError:
        return None
