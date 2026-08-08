"""The exit workflow state machine, compiled from states.yaml.

AGENTS.md: "Every state transition validated against states.yaml, forbidden list
included. A forbidden transition raises, never silently no-ops."

The transition table, the forbidden list and the state set all come from the kit
at import time. :class:`State` is written out explicitly so call sites get real
symbols, and its membership is checked against states.yaml on import — if the
kit changes and this enum does not, the module refuses to start.

Validation order is deliberate: the forbidden list is consulted *first*, so an
entry that also appears in the transition table would still be refused. The two
do not currently overlap, but a forbidden rule that could be silently overridden
by an allowed one would be a trap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import Final

from exit_workflow.domain import spec
from exit_workflow.domain.errors import ForbiddenTransition, WrongState

_WILDCARD: Final = "any"
_FORBIDDEN_GRAMMAR: Final = re.compile(
    r"^(?P<source>\w+)\s*->\s*(?P<target>\w+)(?:\s+without\s+(?P<without>\w+))?$"
)


class State(StrEnum):
    """states.yaml#exit_workflow.states."""

    INITIATED = "INITIATED"
    DOCS_SUBMITTED = "DOCS_SUBMITTED"
    OWNER_NOTIFIED = "OWNER_NOTIFIED"
    INSPECTION_SCHEDULED = "INSPECTION_SCHEDULED"
    INSPECTION_DONE = "INSPECTION_DONE"
    DAMAGE_CONFIRMED = "DAMAGE_CONFIRMED"
    REFUND_PROCESSED = "REFUND_PROCESSED"
    NOC_ISSUED = "NOC_ISSUED"
    COMPLETE = "COMPLETE"
    STALLED = "STALLED"


@dataclass(frozen=True, slots=True)
class Transition:
    """One row of states.yaml#exit_workflow.transitions."""

    source: State
    target: State
    #: The actor states.yaml names for this edge. Authorization is enforced at
    #: the API boundary from api.yaml; this is the *declared* actor, recorded so
    #: that operators can see which party the kit expects to drive the edge. It
    #: is not compared against the audit actor: api.yaml lets ``system|owner``
    #: call /settle, which drives edges states.yaml attributes to ``system``.
    actor: str
    rule: str | None = None
    side_effect: str | None = None
    requires: tuple[str, ...] = ()
    when: str | None = None


@dataclass(frozen=True, slots=True)
class ForbiddenRule:
    """One row of states.yaml#exit_workflow.forbidden."""

    raw: str
    source: State | None  # None == the "any" wildcard
    target: State
    without: State | None

    def matches(self, source: State, target: State) -> bool:
        if target is not self.target:
            return False
        if self.without is not None:
            # "any -> NOC_ISSUED without REFUND_PROCESSED": the edge into
            # ``target`` is forbidden from everywhere except ``without``.
            return source is not self.without
        return self.source is None or source is self.source

    def explain(self) -> str:
        if self.without is not None:
            return (
                f"{self.target} may only be entered from {self.without} "
                f"(states.yaml#forbidden: {self.raw!r})"
            )
        return f"states.yaml#forbidden: {self.raw!r}"


class StateMachine:
    """Validator for exit workflow transitions."""

    def __init__(
        self,
        *,
        initial: State,
        transitions: tuple[Transition, ...],
        forbidden: tuple[ForbiddenRule, ...],
    ) -> None:
        self.initial = initial
        self.transitions = transitions
        self.forbidden = forbidden
        self._by_edge = {(t.source, t.target): t for t in transitions}
        self._by_source: dict[State, tuple[Transition, ...]] = {}
        for transition in transitions:
            self._by_source.setdefault(transition.source, ())
            self._by_source[transition.source] += (transition,)

    def transition_for(self, source: State, target: State) -> Transition | None:
        return self._by_edge.get((source, target))

    def outgoing(self, source: State) -> tuple[Transition, ...]:
        return self._by_source.get(source, ())

    def sources_for(self, target: State) -> tuple[State, ...]:
        return tuple(t.source for t in self.transitions if t.target is target)

    def is_terminal(self, state: State) -> bool:
        """True when states.yaml defines no outgoing edge.

        COMPLETE is terminal by design. STALLED is terminal only because the kit
        defines no way out of it — see blockers.md#B-2.
        """
        return not self.outgoing(state)

    def check(self, source: State, target: State) -> Transition:
        """Validate an edge, returning its spec row.

        :raises ForbiddenTransition: the edge is on states.yaml#forbidden.
        :raises WrongState: the edge is simply not in the transition table.
        """
        for rule in self.forbidden:
            if rule.matches(source, target):
                raise ForbiddenTransition(
                    f"Transition {source} -> {target} is forbidden: {rule.explain()}",
                    current=str(source),
                    expected=[str(s) for s in self.sources_for(target)] or None,
                )

        transition = self._by_edge.get((source, target))
        if transition is None:
            raise WrongState(
                f"Transition {source} -> {target} is not defined in states.yaml.",
                current=str(source),
                expected=[str(t.target) for t in self.outgoing(source)] or None,
            )
        return transition


def _parse_state(name: str, *, context: str) -> State:
    try:
        return State(name)
    except ValueError:
        raise spec.SpecLoadError(
            f"states.yaml {context} references unknown state {name!r}"
        ) from None


def _parse_forbidden(raw: object) -> ForbiddenRule:
    if not isinstance(raw, str):
        raise spec.SpecLoadError(f"states.yaml forbidden entry must be a string, got {raw!r}")
    match = _FORBIDDEN_GRAMMAR.match(raw.strip())
    if match is None:
        # A forbidden rule this module cannot parse must not be quietly dropped:
        # it would turn a prohibition into a permission.
        raise spec.SpecLoadError(
            f"states.yaml forbidden entry {raw!r} does not match the supported grammar "
            "'<state|any> -> <state> [without <state>]'"
        )
    source_name = match.group("source")
    without_name = match.group("without")
    return ForbiddenRule(
        raw=raw.strip(),
        source=None if source_name == _WILDCARD else _parse_state(source_name, context="forbidden"),
        target=_parse_state(match.group("target"), context="forbidden"),
        without=None if without_name is None else _parse_state(without_name, context="forbidden"),
    )


@cache
def exit_workflow_machine() -> StateMachine:
    """Compile states.yaml#exit_workflow into a validator."""
    document = spec.load("states.yaml")
    try:
        definition = document["exit_workflow"]
        declared_states = definition["states"]
        raw_transitions = definition["transitions"]
        initial = definition["initial"]
    except (KeyError, TypeError) as exc:
        raise spec.SpecLoadError(f"states.yaml#exit_workflow is malformed: {exc}") from exc

    if set(declared_states) != {member.value for member in State}:
        raise spec.SpecLoadError(
            "states.yaml#exit_workflow.states has drifted from exit_workflow.domain.states.State: "
            f"spec={sorted(declared_states)} code={sorted(m.value for m in State)}"
        )

    transitions = tuple(
        Transition(
            source=_parse_state(row["from"], context="transitions"),
            target=_parse_state(row["to"], context="transitions"),
            actor=row["actor"],
            rule=row.get("rule"),
            side_effect=row.get("side_effect"),
            requires=tuple(row.get("requires", ())),
            when=row.get("when"),
        )
        for row in raw_transitions
    )
    forbidden = tuple(_parse_forbidden(row) for row in definition.get("forbidden", ()))

    return StateMachine(
        initial=_parse_state(initial, context="initial"),
        transitions=transitions,
        forbidden=forbidden,
    )


def check_transition(source: State, target: State) -> Transition:
    """Module-level shorthand for :meth:`StateMachine.check`."""
    return exit_workflow_machine().check(source, target)
