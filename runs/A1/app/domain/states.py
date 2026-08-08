"""The exit workflow state machine.

The states below are a direct encoding of the ten-step flow in SRS T13, refined with the
inspection-agency sequence from Appendix B (O15) and the settlement sequence from O16.

    T13 step                          state reached
    -------------------------------   ---------------------------------------------
    1  Exit section                   (entry point)
    2  move-out date            \\
    3  reason entry              >--  DRAFT              (created together)
    5  Workflow ID generation   /
    4  document upload               DOCUMENTS_PENDING -> (satisfied)
    6  owner notification            PENDING_OWNER_APPROVAL -> OWNER_APPROVED
    7  inspection scheduling         INSPECTION_SCHEDULING -> INSPECTION_SCHEDULED
                                     -> INSPECTION_COMPLETED
    8  damage review                 DAMAGE_REVIEW
    9  deposit refund                PENDING_SETTLEMENT -> SETTLED
    10 NOC download / completion     NOC_ISSUED -> COMPLETED

Transitions are declared in one table. Nothing in this module may change a workflow's
state except through :func:`assert_can_transition`, which is enforced centrally in
``WorkflowService._transition``.
"""

from __future__ import annotations

from enum import StrEnum

from app.errors import InvalidStateTransition


class ExitWorkflowState(StrEnum):
    DRAFT = "DRAFT"
    DOCUMENTS_PENDING = "DOCUMENTS_PENDING"
    PENDING_OWNER_APPROVAL = "PENDING_OWNER_APPROVAL"
    OWNER_APPROVED = "OWNER_APPROVED"
    INSPECTION_SCHEDULING = "INSPECTION_SCHEDULING"
    INSPECTION_SCHEDULED = "INSPECTION_SCHEDULED"
    INSPECTION_COMPLETED = "INSPECTION_COMPLETED"
    DAMAGE_REVIEW = "DAMAGE_REVIEW"
    PENDING_SETTLEMENT = "PENDING_SETTLEMENT"
    SETTLED = "SETTLED"
    NOC_ISSUED = "NOC_ISSUED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


S = ExitWorkflowState

#: Terminal states. A workflow in one of these no longer blocks new contracts (BR-1).
TERMINAL_STATES: frozenset[ExitWorkflowState] = frozenset(
    {S.COMPLETED, S.CANCELLED, S.REJECTED}
)

#: Active (in-flight) states. Any of these blocks new contracts under BR-1.
ACTIVE_STATES: frozenset[ExitWorkflowState] = frozenset(set(S) - TERMINAL_STATES)

#: States from which a workflow may still be cancelled. Once money has moved (SETTLED and
#: beyond) cancellation is no longer possible — the settlement would have to be reversed.
CANCELLABLE_STATES: frozenset[ExitWorkflowState] = frozenset(
    {
        S.DRAFT,
        S.DOCUMENTS_PENDING,
        S.PENDING_OWNER_APPROVAL,
        S.OWNER_APPROVED,
        S.INSPECTION_SCHEDULING,
        S.INSPECTION_SCHEDULED,
        S.INSPECTION_COMPLETED,
        S.DAMAGE_REVIEW,
        S.PENDING_SETTLEMENT,
    }
)

_TRANSITIONS: dict[ExitWorkflowState, frozenset[ExitWorkflowState]] = {
    S.DRAFT: frozenset({S.DOCUMENTS_PENDING, S.PENDING_OWNER_APPROVAL, S.CANCELLED}),
    S.DOCUMENTS_PENDING: frozenset({S.PENDING_OWNER_APPROVAL, S.CANCELLED}),
    S.PENDING_OWNER_APPROVAL: frozenset({S.OWNER_APPROVED, S.REJECTED, S.CANCELLED}),
    S.OWNER_APPROVED: frozenset({S.INSPECTION_SCHEDULING, S.CANCELLED}),
    # Reschedule loops back to SCHEDULING; a disputed report re-opens the inspection.
    S.INSPECTION_SCHEDULING: frozenset({S.INSPECTION_SCHEDULED, S.CANCELLED}),
    S.INSPECTION_SCHEDULED: frozenset(
        {S.INSPECTION_COMPLETED, S.INSPECTION_SCHEDULING, S.CANCELLED}
    ),
    S.INSPECTION_COMPLETED: frozenset({S.DAMAGE_REVIEW, S.CANCELLED}),
    S.DAMAGE_REVIEW: frozenset({S.PENDING_SETTLEMENT, S.INSPECTION_SCHEDULING, S.CANCELLED}),
    S.PENDING_SETTLEMENT: frozenset({S.SETTLED, S.DAMAGE_REVIEW, S.CANCELLED}),
    S.SETTLED: frozenset({S.NOC_ISSUED}),
    S.NOC_ISSUED: frozenset({S.COMPLETED}),
    S.COMPLETED: frozenset(),
    S.CANCELLED: frozenset(),
    S.REJECTED: frozenset(),
}

#: T13 step number currently in progress, for tenant-app progress display.
_PROGRESS_STEP: dict[ExitWorkflowState, int] = {
    S.DRAFT: 4,
    S.DOCUMENTS_PENDING: 4,
    S.PENDING_OWNER_APPROVAL: 6,
    S.OWNER_APPROVED: 7,
    S.INSPECTION_SCHEDULING: 7,
    S.INSPECTION_SCHEDULED: 7,
    S.INSPECTION_COMPLETED: 8,
    S.DAMAGE_REVIEW: 8,
    S.PENDING_SETTLEMENT: 9,
    S.SETTLED: 9,
    S.NOC_ISSUED: 10,
    S.COMPLETED: 10,
}

TOTAL_STEPS = 10


def allowed_transitions(state: ExitWorkflowState) -> frozenset[ExitWorkflowState]:
    return _TRANSITIONS[state]


def can_transition(current: ExitWorkflowState, target: ExitWorkflowState) -> bool:
    return target in _TRANSITIONS[current]


def assert_can_transition(current: ExitWorkflowState, target: ExitWorkflowState) -> None:
    """Raise :class:`InvalidStateTransition` unless ``current -> target`` is legal."""
    if not can_transition(current, target):
        raise InvalidStateTransition(
            current.value,
            target.value,
            allowed=[s.value for s in _TRANSITIONS[current]],
        )


def is_terminal(state: ExitWorkflowState) -> bool:
    return state in TERMINAL_STATES


def is_active(state: ExitWorkflowState) -> bool:
    return state in ACTIVE_STATES


def progress_step(state: ExitWorkflowState) -> int | None:
    """Which of T13's ten steps is currently in progress (``None`` once aborted)."""
    return _PROGRESS_STEP.get(state)


def _assert_table_is_closed() -> None:
    """Every state must have an entry, and every target must be a known state."""
    missing = set(ExitWorkflowState) - set(_TRANSITIONS)
    if missing:  # pragma: no cover - guards against an incomplete edit
        raise RuntimeError(f"transition table missing states: {sorted(missing)}")
    reachable = {t for targets in _TRANSITIONS.values() for t in targets}
    unknown = reachable - set(ExitWorkflowState)
    if unknown:  # pragma: no cover
        raise RuntimeError(f"transition table references unknown states: {sorted(unknown)}")
    for terminal in TERMINAL_STATES:
        if _TRANSITIONS[terminal]:  # pragma: no cover
            raise RuntimeError(f"terminal state {terminal} must have no outgoing transitions")


_assert_table_is_closed()
