"""The exit workflow state machine, compiled from states.yaml.

AGENTS.md, Conventions: "Every state transition validated against states.yaml, forbidden
list included. A forbidden transition raises, never silently no-ops."

The transition table, the actor of each transition, the ``requires`` field list and the
forbidden list are all read from the YAML — none of them are re-typed here, so a spec
edit changes behaviour without a code edit.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from ..enums import Actor, WorkflowState
from ..errors import ForbiddenTransition, WrongState
from ..spec import load

_ANY: Final = "any"
# "INITIATED -> COMPLETE"  |  "any -> NOC_ISSUED without REFUND_PROCESSED"
_FORBIDDEN_RE: Final = re.compile(
    r"^\s*(?P<from>\w+)\s*->\s*(?P<to>\w+)"
    r"(?:\s+without\s+(?P<without>\w+))?\s*$"
)


@dataclass(frozen=True, slots=True)
class Transition:
    """One row of states.yaml#exit_workflow.transitions."""

    from_state: WorkflowState
    to_state: WorkflowState
    actor: Actor
    requires: tuple[str, ...] = ()
    rule: str | None = None
    side_effect: str | None = None
    when: str | None = None


@dataclass(frozen=True, slots=True)
class ForbiddenRule:
    """One row of states.yaml#exit_workflow.forbidden."""

    raw: str
    to_state: WorkflowState
    from_state: WorkflowState | None  # None == the literal ``any``
    without: WorkflowState | None

    def blocks(
        self,
        from_state: WorkflowState,
        to_state: WorkflowState,
        history: Collection[WorkflowState],
    ) -> bool:
        if to_state is not self.to_state:
            return False
        if self.from_state is not None and from_state is not self.from_state:
            return False
        if self.without is not None:
            # "any -> NOC_ISSUED without REFUND_PROCESSED": forbidden unless the workflow
            # has actually passed through REFUND_PROCESSED. states.yaml, T13 / EXIT-08.
            return self.without not in history and from_state is not self.without
        return True


class StateMachine:
    """Validates transitions against states.yaml. Stateless and safe to share."""

    def __init__(self, document: dict) -> None:
        machine = document["exit_workflow"]
        self.initial = WorkflowState(machine["initial"])
        self.states: frozenset[WorkflowState] = frozenset(
            WorkflowState(s) for s in machine["states"]
        )
        self.transitions: tuple[Transition, ...] = tuple(
            Transition(
                from_state=WorkflowState(row["from"]),
                to_state=WorkflowState(row["to"]),
                actor=Actor(row["actor"]),
                requires=tuple(row.get("requires", ())),
                rule=row.get("rule"),
                side_effect=row.get("side_effect"),
                when=row.get("when"),
            )
            for row in machine["transitions"]
        )
        self.forbidden: tuple[ForbiddenRule, ...] = tuple(
            self._parse_forbidden(raw) for raw in machine["forbidden"]
        )

    @staticmethod
    def _parse_forbidden(raw: str) -> ForbiddenRule:
        match = _FORBIDDEN_RE.match(raw)
        if match is None:  # pragma: no cover - guards a spec edit
            raise RuntimeError(f"unparseable forbidden rule in states.yaml: {raw!r}")
        origin = match.group("from")
        without = match.group("without")
        return ForbiddenRule(
            raw=raw.strip(),
            to_state=WorkflowState(match.group("to")),
            from_state=None if origin == _ANY else WorkflowState(origin),
            without=WorkflowState(without) if without else None,
        )

    def find(self, from_state: WorkflowState, to_state: WorkflowState) -> Transition | None:
        for transition in self.transitions:
            if transition.from_state is from_state and transition.to_state is to_state:
                return transition
        return None

    def outgoing(self, from_state: WorkflowState) -> tuple[Transition, ...]:
        return tuple(t for t in self.transitions if t.from_state is from_state)

    def validate(
        self,
        *,
        from_state: WorkflowState,
        to_state: WorkflowState,
        actor: Actor,
        history: Collection[WorkflowState] = (),
        provided: Iterable[str] = (),
    ) -> Transition:
        """Return the transition, or raise.

        Order matters: the forbidden list is checked before the transition table so that
        a forbidden pair reports as forbidden even if some future spec edit also lists it
        as a transition.
        """
        for rule in self.forbidden:
            if rule.blocks(from_state, to_state, history):
                raise ForbiddenTransition(
                    f"transition {from_state} -> {to_state} is forbidden "
                    f"by states.yaml (forbidden: {rule.raw})",
                    from_state=str(from_state),
                    to_state=str(to_state),
                    forbidden_rule=rule.raw,
                )

        transition = self.find(from_state, to_state)
        if transition is None:
            raise WrongState(
                f"no transition {from_state} -> {to_state} in states.yaml",
                from_state=str(from_state),
                to_state=str(to_state),
                allowed=[str(t.to_state) for t in self.outgoing(from_state)],
            )

        if transition.actor is not actor:
            raise WrongState(
                f"transition {from_state} -> {to_state} is performed by "
                f"{transition.actor}, not {actor}",
                from_state=str(from_state),
                to_state=str(to_state),
                expected_actor=str(transition.actor),
            )

        missing = [field for field in transition.requires if field not in set(provided)]
        if missing:
            raise WrongState(
                f"transition {from_state} -> {to_state} requires {missing}",
                from_state=str(from_state),
                to_state=str(to_state),
                missing=missing,
            )
        return transition


@lru_cache(maxsize=1)
def state_machine() -> StateMachine:
    return StateMachine(load("states.yaml"))
