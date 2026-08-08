"""State transition + audit service.

Every status change in this module goes through :meth:`TransitionService.apply`.
There is no other writer of ``exit_workflows.status``.

* states.yaml is the authority on what is legal (AGENTS.md).
* rules.yaml#EXIT-10 requires an audit row for every state change: actor,
  timestamp, from, to, metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from ..clock import Clock
from ..db.models import ExitWorkflow, ExitWorkflowAudit
from ..domain.states import Actor, State, Transition, load_machine


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is acting. Identity is supplied by the API gateway / auth module;
    session design itself is blocked on risks.md#R3 and is out of this module."""

    id: str
    role: Actor
    scopes: frozenset[str] = field(default_factory=frozenset)


#: The system actor for transitions with no human behind them
#: (states.yaml: actor `system`).
SYSTEM_PRINCIPAL = Principal(id="system", role=Actor.SYSTEM)


class TransitionService:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._machine = load_machine()

    def record_creation(
        self,
        session: AsyncSession,
        workflow: ExitWorkflow,
        actor: Principal,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Audit row for the workflow coming into existence at the initial state.

        states.yaml#exit_workflow.initial is INITIATED; there is no transition
        into it, so it is recorded with a null `from` (rules.yaml#EXIT-10).
        """
        if workflow.status is not self._machine.initial:
            raise ValueError(
                f"creation must be recorded at {self._machine.initial}, got {workflow.status}"
            )
        session.add(
            ExitWorkflowAudit(
                workflow_id=workflow.id,
                actor_id=actor.id,
                actor_role=actor.role.value,
                from_state=None,
                to_state=workflow.status,
                metadata_={"rule": "EXIT-02", **(metadata or {})},
                occurred_at=self._clock.now_utc(),
            )
        )

    async def apply(
        self,
        session: AsyncSession,
        workflow: ExitWorkflow,
        to_state: State,
        actor: Principal,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> Transition:
        """Validate against states.yaml, move the workflow, write the audit row.

        Raises ForbiddenTransition for anything on states.yaml#forbidden and
        WrongState for anything simply not declared. Never a silent no-op
        (AGENTS.md).
        """
        from_state = workflow.status
        # AGENTS.md — every transition validated against states.yaml, forbidden included.
        transition = self._machine.validate(from_state, to_state, actor.role)

        workflow.status = to_state
        workflow.updated_at = self._clock.now_utc()

        # rules.yaml#EXIT-10 — append-only audit row per state change.
        session.add(
            ExitWorkflowAudit(
                workflow_id=workflow.id,
                actor_id=actor.id,
                actor_role=actor.role.value,
                from_state=from_state,
                to_state=to_state,
                metadata_={
                    "rule": transition.rule,
                    "side_effect": transition.side_effect,
                    **(metadata or {}),
                },
                occurred_at=self._clock.now_utc(),
            )
        )
        return transition
