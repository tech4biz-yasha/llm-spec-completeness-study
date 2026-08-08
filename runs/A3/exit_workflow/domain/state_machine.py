"""The exit workflow state machine.

The transition table is the single authority on what may happen next and who
may cause it. Services never compare statuses ad hoc; they call
:func:`assert_can_transition` and let this module raise.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from exit_workflow.core.errors import ForbiddenError, InvalidTransitionError
from exit_workflow.core.security import Role
from exit_workflow.domain.enums import TERMINAL_STATUSES, ExitWorkflowStatus as S

#: ``None`` in an actor set means "system-initiated only" is also allowed; the
#: service layer passes ``actor_role=None`` for automatic transitions such as
#: NOC issuance immediately after a successful payout.


@dataclass(frozen=True, slots=True)
class Transition:
    source: S
    target: S
    actors: frozenset[Role]
    description: str


def _t(source: S, target: S, actors: set[Role], description: str) -> Transition:
    # ADMIN/SERVICE may drive any declared transition — they are the operational
    # escape hatch and every use is audited.
    return Transition(source, target, frozenset(actors | {Role.ADMIN, Role.SERVICE}), description)


_TRANSITIONS: tuple[Transition, ...] = (
    _t(S.INITIATED, S.PENDING_OWNER_APPROVAL, {Role.TENANT},
       "Tenant submits the exit request; owner is notified (T13 step 5)."),
    _t(S.INITIATED, S.CANCELLED, {Role.TENANT},
       "Tenant abandons the draft exit request."),

    _t(S.PENDING_OWNER_APPROVAL, S.OWNER_APPROVED, {Role.OWNER},
       "Owner approves the exit (Appendix B O15)."),
    _t(S.PENDING_OWNER_APPROVAL, S.REJECTED, {Role.OWNER},
       "Owner rejects the exit request with a reason."),
    _t(S.PENDING_OWNER_APPROVAL, S.CANCELLED, {Role.TENANT},
       "Tenant withdraws the request before the owner decides."),

    _t(S.OWNER_APPROVED, S.INSPECTION_REQUESTED, {Role.OWNER},
       "Inspection request emailed to the registered agency (O15)."),
    _t(S.OWNER_APPROVED, S.CANCELLED, {Role.TENANT, Role.OWNER},
       "Exit called off before an inspection was requested."),

    _t(S.INSPECTION_REQUESTED, S.INSPECTION_SCHEDULED, {Role.OWNER, Role.TENANT},
       "Owner or tenant selects one of the agency's proposed dates (O15)."),
    _t(S.INSPECTION_REQUESTED, S.OWNER_APPROVED, {Role.OWNER, Role.INSPECTION_AGENCY},
       "Inspection request cancelled; another agency may be engaged."),
    _t(S.INSPECTION_REQUESTED, S.CANCELLED, {Role.OWNER},
       "Exit called off while awaiting agency availability."),

    _t(S.INSPECTION_SCHEDULED, S.INSPECTION_COMPLETED, {Role.INSPECTION_AGENCY},
       "Agency records that the inspection took place."),
    _t(S.INSPECTION_SCHEDULED, S.INSPECTION_REQUESTED, {Role.INSPECTION_AGENCY, Role.OWNER},
       "Appointment released; agency re-proposes availability."),
    _t(S.INSPECTION_SCHEDULED, S.OWNER_APPROVED, {Role.OWNER, Role.INSPECTION_AGENCY},
       "Inspection cancelled outright."),
    _t(S.INSPECTION_SCHEDULED, S.CANCELLED, {Role.OWNER},
       "Exit called off before the inspection happened."),

    _t(S.INSPECTION_COMPLETED, S.DAMAGE_REVIEW, {Role.INSPECTION_AGENCY},
       "Damage report uploaded; owner and tenant review it (T13 step 7)."),

    _t(S.DAMAGE_REVIEW, S.SETTLEMENT_PENDING, {Role.OWNER},
       "Owner finalises the deduction; settlement is ready to pay (O16)."),
    _t(S.DAMAGE_REVIEW, S.OWNER_APPROVED, set(),
       "Administrator orders a re-inspection."),
    _t(S.DAMAGE_REVIEW, S.CANCELLED, set(),
       "Administrator terminates the exit during damage review."),

    _t(S.SETTLEMENT_PENDING, S.SETTLEMENT_COMPLETED, {Role.OWNER},
       "Owner pays the deposit balance (O16 'Pay Deposit')."),
    _t(S.SETTLEMENT_PENDING, S.DAMAGE_REVIEW, set(),
       "Administrator reopens damage review before any money moves."),
    _t(S.SETTLEMENT_PENDING, S.CANCELLED, set(),
       "Administrator terminates the exit before payout."),

    _t(S.SETTLEMENT_COMPLETED, S.NOC_ISSUED, set(),
       "Exit NOC auto-generated upon payment (O16)."),
    _t(S.NOC_ISSUED, S.COMPLETED, set(),
       "Workflow completes; BR-1 lock released (T13 step 10)."),
)

TRANSITIONS: MappingProxyType[tuple[S, S], Transition] = MappingProxyType(
    {(t.source, t.target): t for t in _TRANSITIONS}
)

_ADJACENCY: MappingProxyType[S, frozenset[S]] = MappingProxyType(
    {
        status: frozenset(t.target for t in _TRANSITIONS if t.source is status)
        for status in S
    }
)


def next_statuses(current: S) -> frozenset[S]:
    return _ADJACENCY[current]


def is_terminal(status: S) -> bool:
    return status in TERMINAL_STATUSES


def allowed_targets_for(current: S, role: Role | None) -> frozenset[S]:
    """Targets reachable by ``role`` (``None`` = system) from ``current``."""

    return frozenset(
        target
        for target in _ADJACENCY[current]
        if role is None or role in TRANSITIONS[(current, target)].actors
    )


def assert_can_transition(current: S, target: S, actor_role: Role | None) -> Transition:
    """Validate a state change, or raise.

    ``actor_role=None`` denotes a system-driven transition (NOC issuance,
    completion) and bypasses the actor check but never the topology check.
    """

    if is_terminal(current):
        raise InvalidTransitionError(
            current.value,
            target.value,
            f"Exit workflow is already {current.value}; no further transitions are possible.",
        )
    transition = TRANSITIONS.get((current, target))
    if transition is None:
        raise InvalidTransitionError(current.value, target.value)
    if actor_role is not None and actor_role not in transition.actors:
        raise ForbiddenError(
            f"Role {actor_role.value} may not move an exit workflow from "
            f"{current.value} to {target.value}.",
            extra={"current_status": current.value, "requested_status": target.value},
        )
    return transition


def describe_machine() -> list[dict[str, object]]:
    """Machine-readable transition table (exposed at ``GET /meta/state-machine``)."""

    return [
        {
            "from": t.source.value,
            "to": t.target.value,
            "actors": sorted(r.value for r in t.actors),
            "description": t.description,
        }
        for t in _TRANSITIONS
    ]
