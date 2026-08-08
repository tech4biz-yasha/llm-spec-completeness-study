"""The single place a workflow's status changes.

AGENTS.md: "Every state transition validated against states.yaml, forbidden list
included. A forbidden transition raises, never silently no-ops." and
rules.yaml#EXIT-10: "Every state change writes an audit row."

Both are guaranteed here rather than in each service: no other code assigns
``ExitWorkflow.status``, so there is no path that can move a workflow without
validation and an audit row.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import ExitWorkflow
from exit_workflow.domain.enums import ActorRole
from exit_workflow.domain.states import State, Transition, check_transition
from exit_workflow.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)


async def apply_transition(
    session: AsyncSession,
    workflow: ExitWorkflow,
    target: State,
    *,
    actor_type: ActorRole,
    actor_id: str | None,
    metadata: dict[str, Any] | None = None,
) -> Transition:
    """Validate, apply and audit one state change.

    The audit row joins the caller's transaction, so a step described as "IN ONE
    TRANSACTION" in algorithm.md commits its state change and its audit row
    together.

    :raises ForbiddenTransition: states.yaml#forbidden.
    :raises WrongState: the edge is not in states.yaml#transitions.
    """
    source = State(workflow.status)
    transition = check_transition(source, target)

    workflow.status = str(target)
    AuditRepository(session).append(
        workflow_id=workflow.id,
        actor_type=actor_type,
        actor_id=actor_id,
        from_state=source,
        to_state=target,
        rule_id=transition.rule,
        metadata=metadata or {},
    )
    logger.info(
        "exit workflow %s: %s -> %s by %s (rule %s)",
        workflow.id,
        source,
        target,
        actor_type,
        transition.rule or "-",
    )
    return transition
