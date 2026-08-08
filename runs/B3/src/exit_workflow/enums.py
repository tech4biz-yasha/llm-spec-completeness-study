"""Enumerations that are fixed by the spec kit.

Anything the kit does not enumerate is NOT enumerated here. The exit-reason list is the
notable case: risks.md (Appendix A carry-over) records "Reference data dictionary,
specifically exit reasons (blocks the ExitWorkflow enum)" as an open item, so this module
has no ExitReason enum. Reasons are validated against injected reference data instead —
see ``ports.reference`` and blockers.md#B-2.
"""

from __future__ import annotations

from enum import StrEnum

from .spec import load

_MACHINE = load("states.yaml")["exit_workflow"]


class WorkflowState(StrEnum):
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


if {s.value for s in WorkflowState} != set(_MACHINE["states"]):  # pragma: no cover
    raise RuntimeError("WorkflowState drifted from states.yaml#exit_workflow.states")

INITIAL_STATE = WorkflowState(_MACHINE["initial"])


class Actor(StrEnum):
    """Actors named in states.yaml transitions and api.yaml ``authz`` lines.

    ``inspection_agency`` (api.yaml) and ``inspector`` (states.yaml) name the same party;
    ``Actor.normalize`` maps the api.yaml spelling onto the states.yaml one so the
    transition table stays the single source of truth. See blockers.md#B-9.
    """

    TENANT = "tenant"
    OWNER = "owner"
    INSPECTOR = "inspector"
    SYSTEM = "system"
    ADMIN = "admin"

    @classmethod
    def normalize(cls, raw: str) -> Actor:
        if raw == "inspection_agency":
            return cls.INSPECTOR
        return cls(raw)


class ContractStatus(StrEnum):
    """Only ACTIVE is named by the kit (algorithm.md#1); the rest are carried as opaque."""

    ACTIVE = "ACTIVE"


class PaymentType(StrEnum):
    """rules.yaml#EXIT-08."""

    DEPOSIT_REFUND = "DEPOSIT_REFUND"


class PaymentStatus(StrEnum):
    """algorithm.md#11 — SUCCEEDED proceeds; PENDING and FAILED hold."""

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OutboxStatus(StrEnum):
    """rules.yaml#EXIT-04 — 5 attempts with exponential backoff, then dead-letter."""

    PENDING = "PENDING"
    SENT = "SENT"
    DEAD_LETTER = "DEAD_LETTER"


class AdminTaskType(StrEnum):
    """rules.yaml#EXIT-05 (stalled workflow) and #EXIT-04 (dead-lettered notification)."""

    EXIT_WORKFLOW_STALLED = "EXIT_WORKFLOW_STALLED"
    OWNER_NOTIFICATION_DEAD_LETTER = "OWNER_NOTIFICATION_DEAD_LETTER"


class AdminTaskStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
