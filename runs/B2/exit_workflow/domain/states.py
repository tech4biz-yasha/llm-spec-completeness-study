"""The exit workflow state machine.

AGENTS.md: "Every state transition validated against states.yaml, forbidden list
included. A forbidden transition raises, never silently no-ops."

The machine is *loaded from* ``exit_workflow/spec/states.yaml`` (a byte copy of
the kit's states.yaml) rather than transcribed into Python, so the spec file
stays the single source of truth and drift is impossible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from typing import Final

import yaml

from ..errors import ForbiddenTransition, WrongState

_SPEC_PACKAGE: Final[str] = "exit_workflow.spec"
_SPEC_FILE: Final[str] = "states.yaml"
_MACHINE_KEY: Final[str] = "exit_workflow"


class State(StrEnum):
    """states.yaml#exit_workflow.states — validated against the file at import."""

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


class Actor(StrEnum):
    """Actors named by states.yaml transitions and api.yaml authz lines."""

    TENANT = "tenant"
    OWNER = "owner"
    SYSTEM = "system"
    INSPECTOR = "inspector"
    INSPECTION_AGENCY = "inspection_agency"
    ADMIN = "admin"


# api.yaml calls the inspection-report principal `inspection_agency`;
# states.yaml calls the same actor `inspector`. Treated as one actor.
_ACTOR_ALIASES: Final[dict[Actor, Actor]] = {Actor.INSPECTION_AGENCY: Actor.INSPECTOR}


def canonical_actor(actor: Actor) -> Actor:
    return _ACTOR_ALIASES.get(actor, actor)


@dataclass(frozen=True, slots=True)
class Transition:
    source: State
    target: State
    actor: Actor
    requires: tuple[str, ...] = ()
    side_effect: str | None = None
    when: str | None = None
    rule: str | None = None


@dataclass(frozen=True, slots=True)
class ForbiddenPair:
    """A literal ``A -> B`` entry of states.yaml#forbidden."""

    source: State
    target: State


@dataclass(frozen=True, slots=True)
class ForbiddenUnless:
    """``any -> X without Y`` — X may only be entered from Y.

    states.yaml: ``any -> NOC_ISSUED without REFUND_PROCESSED`` (T13 order, EXIT-08).
    """

    target: State
    required_source: State


_ANY_WITHOUT = re.compile(r"^any\s*->\s*(?P<target>\w+)\s+without\s+(?P<required>\w+)$")
_PAIR = re.compile(r"^(?P<source>\w+)\s*->\s*(?P<target>\w+)$")


class StateMachine:
    """Validates every transition against states.yaml, forbidden list included."""

    def __init__(
        self,
        *,
        initial: State,
        states: frozenset[State],
        transitions: tuple[Transition, ...],
        forbidden_pairs: frozenset[ForbiddenPair],
        forbidden_unless: tuple[ForbiddenUnless, ...],
    ) -> None:
        self.initial = initial
        self.states = states
        self.transitions = transitions
        self.forbidden_pairs = forbidden_pairs
        self.forbidden_unless = forbidden_unless

    def find(self, source: State, target: State, actor: Actor | None = None) -> Transition | None:
        wanted = canonical_actor(actor) if actor is not None else None
        for t in self.transitions:
            if t.source is source and t.target is target:
                if wanted is None or canonical_actor(t.actor) is wanted:
                    return t
        return None

    def targets_from(self, source: State) -> frozenset[State]:
        return frozenset(t.target for t in self.transitions if t.source is source)

    def validate(self, source: State, target: State, actor: Actor) -> Transition:
        """Return the declared transition, or raise. Never silently no-ops.

        Order matters: the forbidden list is checked first, so a move that the
        spec explicitly names as illegal always raises ForbiddenTransition even
        if some other reading would allow it. states.yaml#exit_workflow.forbidden
        """
        if ForbiddenPair(source, target) in self.forbidden_pairs:
            raise ForbiddenTransition(
                f"transition {source} -> {target} is forbidden by states.yaml",
                from_state=source,
                to_state=target,
            )

        for rule in self.forbidden_unless:
            # states.yaml: any -> NOC_ISSUED without REFUND_PROCESSED (EXIT-08, T13 order)
            if target is rule.target and source is not rule.required_source:
                raise ForbiddenTransition(
                    f"transition {source} -> {target} is forbidden by states.yaml: "
                    f"{target} requires {rule.required_source}",
                    from_state=source,
                    to_state=target,
                )

        transition = self.find(source, target, actor)
        if transition is None:
            if self.find(source, target) is not None:
                raise WrongState(
                    f"actor {actor} may not perform {source} -> {target}",
                    from_state=source,
                    to_state=target,
                )
            raise WrongState(
                f"transition {source} -> {target} is not declared in states.yaml",
                from_state=source,
                to_state=target,
            )
        return transition


def _load_raw() -> dict:
    text = resources.files(_SPEC_PACKAGE).joinpath(_SPEC_FILE).read_text(encoding="utf-8")
    return yaml.safe_load(text)[_MACHINE_KEY]


@lru_cache(maxsize=1)
def load_machine() -> StateMachine:
    """Parse states.yaml into a StateMachine. Fails loudly on any mismatch."""
    raw = _load_raw()

    states = frozenset(State(s) for s in raw["states"])
    declared = frozenset(State)
    if states != declared:
        raise RuntimeError(
            f"State enum and states.yaml disagree: only-in-yaml={states - declared}, "
            f"only-in-code={declared - states}"
        )

    transitions = tuple(
        Transition(
            source=State(t["from"]),
            target=State(t["to"]),
            actor=Actor(t["actor"]),
            requires=tuple(t.get("requires", ())),
            side_effect=t.get("side_effect"),
            when=t.get("when"),
            rule=t.get("rule"),
        )
        for t in raw["transitions"]
    )

    pairs: set[ForbiddenPair] = set()
    unless: list[ForbiddenUnless] = []
    for entry in raw.get("forbidden", ()):
        text = str(entry).split("#", 1)[0].strip()
        if m := _ANY_WITHOUT.match(text):
            unless.append(ForbiddenUnless(State(m["target"]), State(m["required"])))
            continue
        if m := _PAIR.match(text):
            pairs.add(ForbiddenPair(State(m["source"]), State(m["target"])))
            continue
        # An unparsed forbidden entry would silently permit an illegal move.
        raise RuntimeError(f"unparsable states.yaml forbidden entry: {entry!r}")

    return StateMachine(
        initial=State(raw["initial"]),
        states=states,
        transitions=transitions,
        forbidden_pairs=frozenset(pairs),
        forbidden_unless=tuple(unless),
    )


#: Terminal states — no declared outgoing transition.
def terminal_states() -> frozenset[State]:
    machine = load_machine()
    return frozenset(s for s in machine.states if not machine.targets_from(s))
