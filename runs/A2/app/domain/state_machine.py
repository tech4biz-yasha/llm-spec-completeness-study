"""The exit workflow state machine.

The transition table is the single source of truth for "what can happen next and who is
allowed to trigger it". Services never mutate ``ExitWorkflow.state`` directly -- they go
through :func:`assert_transition_allowed`, which keeps authorisation and sequencing in
one auditable place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from app.core.errors import AuthorizationError, IllegalTransitionError
from app.domain.enums import ActorRole, ExitWorkflowState as S


@dataclass(frozen=True, slots=True)
class Transition:
    source: S
    target: S
    #: Roles permitted to trigger this transition. ADMIN is granted implicitly.
    actors: frozenset[ActorRole]
    #: Short identifier used in the audit trail and emitted events.
    action: str
    description: str = ""


def _t(
    source: S,
    target: S,
    action: str,
    actors: Iterable[ActorRole],
    description: str = "",
) -> Transition:
    return Transition(source, target, frozenset(actors), action, description)


_TENANT = (ActorRole.TENANT,)
_OWNER = (ActorRole.OWNER,)
_AGENCY = (ActorRole.INSPECTION_AGENCY,)
_TENANT_OR_OWNER = (ActorRole.TENANT, ActorRole.OWNER)
_SYSTEM = (ActorRole.SYSTEM,)


TRANSITIONS: tuple[Transition, ...] = (
    # --- T13 steps 5-6: submit, generating the workflow ID and notifying the owner.
    _t(S.DRAFT, S.SUBMITTED, "submit", _TENANT, "Tenant submits the exit request"),
    _t(S.DRAFT, S.CANCELLED, "cancel", _TENANT, "Tenant abandons the draft"),
    _t(S.DRAFT, S.EXPIRED, "expire", _SYSTEM, "Draft abandoned past the grace period"),
    # --- O15: owner approves, which triggers the inspection-agency request.
    _t(S.SUBMITTED, S.OWNER_APPROVED, "owner_approve", _OWNER, "Owner approves the exit"),
    _t(S.SUBMITTED, S.REJECTED, "owner_reject", _OWNER, "Owner rejects the exit request"),
    _t(S.SUBMITTED, S.DRAFT, "withdraw", _TENANT, "Tenant pulls the request back to draft"),
    _t(S.SUBMITTED, S.CANCELLED, "cancel", _TENANT_OR_OWNER, "Exit request cancelled"),
    # --- O15: agency proposes dates, a date is selected, inspection occurs.
    _t(
        S.OWNER_APPROVED,
        S.INSPECTION_SLOTS_PROPOSED,
        "propose_inspection_slots",
        _AGENCY,
        "Agency responds with available dates",
    ),
    _t(S.OWNER_APPROVED, S.CANCELLED, "cancel", _TENANT_OR_OWNER),
    _t(
        S.INSPECTION_SLOTS_PROPOSED,
        S.INSPECTION_SCHEDULED,
        "select_inspection_slot",
        _TENANT_OR_OWNER,
        "Owner/tenant select the inspection date",
    ),
    _t(
        S.INSPECTION_SLOTS_PROPOSED,
        S.INSPECTION_SLOTS_PROPOSED,
        "propose_inspection_slots",
        _AGENCY,
        "Agency revises the offered dates",
    ),
    _t(S.INSPECTION_SLOTS_PROPOSED, S.CANCELLED, "cancel", _TENANT_OR_OWNER),
    _t(
        S.INSPECTION_SCHEDULED,
        S.INSPECTION_SLOTS_PROPOSED,
        "reschedule_inspection",
        (ActorRole.TENANT, ActorRole.OWNER, ActorRole.INSPECTION_AGENCY),
        "Scheduled inspection is put back out for new dates",
    ),
    _t(
        S.INSPECTION_SCHEDULED,
        S.INSPECTION_COMPLETED,
        "submit_inspection_report",
        _AGENCY,
        "Agency uploads the damage report with photos",
    ),
    _t(S.INSPECTION_SCHEDULED, S.CANCELLED, "cancel", _TENANT_OR_OWNER),
    # --- T13 step 8: damage review.
    _t(
        S.INSPECTION_COMPLETED,
        S.DAMAGE_REVIEW,
        "open_damage_review",
        (ActorRole.OWNER, ActorRole.SYSTEM),
        "Damage assessment opened for owner adjustment and tenant dispute",
    ),
    # --- T13 step 9: settlement.
    _t(
        S.DAMAGE_REVIEW,
        S.SETTLEMENT_PENDING,
        "finalise_settlement",
        _OWNER,
        "Deductions finalised; deposit minus damage awaits payment",
    ),
    _t(
        S.DAMAGE_REVIEW,
        S.INSPECTION_SLOTS_PROPOSED,
        "request_reinspection",
        _OWNER,
        "Dispute escalated to a re-inspection",
    ),
    _t(
        S.SETTLEMENT_PENDING,
        S.SETTLEMENT_PROCESSING,
        "pay_deposit",
        _OWNER,
        "Owner clicks 'Pay Deposit'; payout submitted to the provider",
    ),
    _t(
        S.SETTLEMENT_PENDING,
        S.DAMAGE_REVIEW,
        "reopen_damage_review",
        _OWNER,
        "Owner reopens the assessment before paying",
    ),
    _t(
        S.SETTLEMENT_PROCESSING,
        S.SETTLEMENT_COMPLETED,
        "settlement_succeeded",
        _SYSTEM,
        "Payment provider confirmed the payout",
    ),
    _t(
        S.SETTLEMENT_PROCESSING,
        S.SETTLEMENT_PENDING,
        "settlement_failed",
        _SYSTEM,
        "Payout failed; owner may retry",
    ),
    # --- T13 step 10: NOC, then completion.
    _t(
        S.SETTLEMENT_COMPLETED,
        S.NOC_ISSUED,
        "issue_noc",
        _SYSTEM,
        "Digital Exit NOC auto-generated upon payment",
    ),
    _t(
        S.NOC_ISSUED,
        S.COMPLETED,
        "complete",
        (ActorRole.TENANT, ActorRole.OWNER, ActorRole.SYSTEM),
        "Workflow completion; releases the BR-1 contract lock",
    ),
)


_BY_SOURCE: dict[S, tuple[Transition, ...]] = {}
for _tr in TRANSITIONS:
    _BY_SOURCE.setdefault(_tr.source, ())
    _BY_SOURCE[_tr.source] += (_tr,)

_BY_ACTION: dict[tuple[S, str], Transition] = {(t.source, t.action): t for t in TRANSITIONS}


def allowed_targets(state: S) -> list[S]:
    """Every state reachable from ``state`` in one step."""
    return sorted({t.target for t in _BY_SOURCE.get(state, ())})


def available_actions(state: S, role: ActorRole) -> list[str]:
    """Actions ``role`` may trigger from ``state`` -- drives the client's action buttons."""
    return sorted(
        {
            t.action
            for t in _BY_SOURCE.get(state, ())
            if role is ActorRole.ADMIN or role in t.actors
        }
    )


def find_transition(source: S, action: str) -> Transition | None:
    return _BY_ACTION.get((source, action))


def assert_transition_allowed(source: S, action: str, role: ActorRole) -> Transition:
    """Validate an action against the state machine.

    Raises :class:`IllegalTransitionError` if the action is not defined for ``source``,
    and :class:`AuthorizationError` if the role may not trigger it.
    """
    transition = _BY_ACTION.get((source, action))
    if transition is None:
        raise IllegalTransitionError(
            current=source.value,
            requested=action,
            allowed=sorted({t.action for t in _BY_SOURCE.get(source, ())}),
        )
    if role is not ActorRole.ADMIN and role not in transition.actors:
        raise AuthorizationError(
            f"Role {role.value} may not perform '{action}' on an exit workflow "
            f"in state {source.value}.",
            details={
                "action": action,
                "state": source.value,
                "allowed_roles": sorted(r.value for r in transition.actors),
            },
        )
    return transition


@dataclass(frozen=True, slots=True)
class StateDescriptor:
    """Client-facing description of where a workflow stands in the 10-step flow."""

    state: S
    step: int
    label: str
    tenant_hint: str = ""
    owner_hint: str = ""
    blocking: bool = field(default=True)


#: ``step`` maps onto the SRS T13 ten-step flow for progress indicators.
STATE_DESCRIPTORS: dict[S, StateDescriptor] = {
    S.DRAFT: StateDescriptor(
        S.DRAFT, 1, "Draft",
        "Add your move-out date, reason and documents, then submit.",
        "",
    ),
    S.SUBMITTED: StateDescriptor(
        S.SUBMITTED, 5, "Awaiting owner approval",
        "Your request has been sent to the owner.",
        "Review and approve this exit request.",
    ),
    S.OWNER_APPROVED: StateDescriptor(
        S.OWNER_APPROVED, 6, "Inspection requested",
        "The inspection agency has been contacted for available dates.",
        "The inspection agency has been contacted for available dates.",
    ),
    S.INSPECTION_SLOTS_PROPOSED: StateDescriptor(
        S.INSPECTION_SLOTS_PROPOSED, 7, "Choose an inspection date",
        "Pick one of the offered inspection slots.",
        "Pick one of the offered inspection slots.",
    ),
    S.INSPECTION_SCHEDULED: StateDescriptor(
        S.INSPECTION_SCHEDULED, 7, "Inspection scheduled",
        "Be available at the property at the scheduled time.",
        "The inspection is booked.",
    ),
    S.INSPECTION_COMPLETED: StateDescriptor(
        S.INSPECTION_COMPLETED, 8, "Inspection report received",
        "The damage report is being reviewed.",
        "Review the damage report and adjust the charges.",
    ),
    S.DAMAGE_REVIEW: StateDescriptor(
        S.DAMAGE_REVIEW, 8, "Damage review",
        "Review the assessed damages; you may raise a dispute.",
        "Adjust the assessed charges and finalise the settlement.",
    ),
    S.SETTLEMENT_PENDING: StateDescriptor(
        S.SETTLEMENT_PENDING, 9, "Awaiting deposit payment",
        "Your refund is awaiting release by the owner.",
        "Click 'Pay Deposit' to release the refund (deposit minus damage).",
    ),
    S.SETTLEMENT_PROCESSING: StateDescriptor(
        S.SETTLEMENT_PROCESSING, 9, "Refund in progress",
        "Your refund is being processed by the payment provider.",
        "The payout has been submitted to the payment provider.",
    ),
    S.SETTLEMENT_COMPLETED: StateDescriptor(
        S.SETTLEMENT_COMPLETED, 9, "Refund paid",
        "Your refund has been paid. Your NOC is being generated.",
        "The refund has been paid.",
    ),
    S.NOC_ISSUED: StateDescriptor(
        S.NOC_ISSUED, 10, "NOC ready",
        "Download your Exit NOC, then close the workflow.",
        "The Exit NOC has been issued to the tenant.",
    ),
    S.COMPLETED: StateDescriptor(
        S.COMPLETED, 10, "Completed",
        "This exit is complete.",
        "This exit is complete. The property can be re-let.",
        blocking=False,
    ),
    S.REJECTED: StateDescriptor(
        S.REJECTED, 5, "Rejected by owner",
        "The owner rejected this request. Contact them for details.",
        "You rejected this exit request.",
        blocking=False,
    ),
    S.CANCELLED: StateDescriptor(
        S.CANCELLED, 0, "Cancelled", "This request was cancelled.", "This request was cancelled.",
        blocking=False,
    ),
    S.EXPIRED: StateDescriptor(
        S.EXPIRED, 0, "Expired",
        "This draft expired. Start a new exit request.",
        "",
        blocking=False,
    ),
}


def describe(state: S) -> StateDescriptor:
    return STATE_DESCRIPTORS[state]
