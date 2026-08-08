"""The single write path for workflow state.

Nothing else in this module assigns ``ExitWorkflow.status``. Every change goes through
``apply_transition``, which validates against states.yaml (forbidden list included) and
writes the rules.yaml#EXIT-10 audit row in the same transaction as the status change.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import UTC
from ..db.models import ExitWorkflow, ExitWorkflowAudit
from ..domain.states import Transition, state_machine
from ..enums import Actor, WorkflowState
from ..errors import WorkflowNotFound


def load_for_update(session: Session, workflow_id: str) -> ExitWorkflow:
    """Load the workflow and hold a row lock for the rest of the transaction.

    edges.yaml#X-005 — concurrent settlement attempts serialize here.
    """
    workflow = session.execute(
        select(ExitWorkflow).where(ExitWorkflow.id == workflow_id).with_for_update()
    ).scalar_one_or_none()
    if workflow is None:
        raise WorkflowNotFound(f"exit workflow {workflow_id} not found", workflow_id=workflow_id)
    return workflow


def state_history(session: Session, workflow_id: str) -> set[WorkflowState]:
    """Every state the workflow has occupied, read from the append-only audit trail.

    This is what makes the states.yaml rule "any -> NOC_ISSUED without REFUND_PROCESSED"
    enforceable: it is a statement about history, not about the immediate predecessor.
    """
    rows = session.execute(
        select(ExitWorkflowAudit.to_state).where(ExitWorkflowAudit.workflow_id == workflow_id)
    ).scalars()
    return {WorkflowState(row) for row in rows}


def record_audit(
    session: Session,
    *,
    workflow_id: str,
    actor: Actor,
    actor_id: str | None,
    from_state: WorkflowState | None,
    to_state: WorkflowState,
    rule_id: str | None,
    metadata: dict[str, Any] | None = None,
    occurred_at: datetime,
) -> ExitWorkflowAudit:
    """rules.yaml#EXIT-10 — actor, timestamp, from, to, metadata. Append-only."""
    entry = ExitWorkflowAudit(
        workflow_id=workflow_id,
        actor_id=actor_id,
        actor_role=str(actor),
        from_state=str(from_state) if from_state is not None else None,
        to_state=str(to_state),
        rule_id=rule_id,
        meta=metadata or {},
        created_at=occurred_at.astimezone(UTC),
    )
    session.add(entry)
    return entry


def apply_transition(
    session: Session,
    workflow: ExitWorkflow,
    *,
    to_state: WorkflowState,
    actor: Actor,
    actor_id: str | None,
    occurred_at: datetime,
    metadata: dict[str, Any] | None = None,
    provided: Iterable[str] = (),
) -> Transition:
    """Validate against states.yaml, move the workflow, write the audit row.

    A forbidden or unknown transition raises (ForbiddenTransition / WrongState); it is
    never a silent no-op. AGENTS.md, Conventions.
    """
    from_state = workflow.state
    transition = state_machine().validate(
        from_state=from_state,
        to_state=to_state,
        actor=actor,
        history=state_history(session, workflow.id) | {from_state},
        provided=provided,
    )
    workflow.status = str(to_state)
    record_audit(
        session,
        workflow_id=workflow.id,
        actor=actor,
        actor_id=actor_id,
        from_state=from_state,
        to_state=to_state,
        rule_id=transition.rule,
        metadata=metadata,
        occurred_at=occurred_at,
    )
    return transition
