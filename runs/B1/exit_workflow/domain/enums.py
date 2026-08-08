"""Vocabularies the kit fixes outside states.yaml."""

from __future__ import annotations

from enum import StrEnum


class ActorRole(StrEnum):
    """Parties named in api.yaml ``authz`` lines and states.yaml ``actor`` fields.

    states.yaml calls the inspection party ``inspector`` on the
    INSPECTION_SCHEDULED -> INSPECTION_DONE edge; api.yaml calls the same party
    ``inspection_agency`` on /inspection-report. They are one role;
    :meth:`from_spec` maps both spellings onto :attr:`INSPECTION_AGENCY`.
    """

    TENANT = "tenant"
    OWNER = "owner"
    INSPECTION_AGENCY = "inspection_agency"
    SYSTEM = "system"
    ADMIN = "admin"

    @classmethod
    def from_spec(cls, name: str) -> "ActorRole":
        if name == "inspector":
            return cls.INSPECTION_AGENCY
        return cls(name)


class ContractStatus(StrEnum):
    """Contract states this module reads. ACTIVE is required by rules.yaml#EXIT-01."""

    ACTIVE = "ACTIVE"


class PaymentType(StrEnum):
    """rules.yaml#EXIT-08 — the refund is a payment of this type."""

    DEPOSIT_REFUND = "DEPOSIT_REFUND"


class PaymentStatus(StrEnum):
    """Gateway outcomes named in algorithm.md step 11 and edges.yaml#X-004."""

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class OutboxStatus(StrEnum):
    """Lifecycle of a queued event (rules.yaml#EXIT-04)."""

    PENDING = "PENDING"
    SENT = "SENT"
    DEAD_LETTER = "DEAD_LETTER"


class AdminTaskType(StrEnum):
    """Admin work items the kit requires the module to open."""

    #: rules.yaml#EXIT-05 — inspection not scheduled within 30 days of move-out.
    EXIT_STALLED = "EXIT_STALLED"
    #: rules.yaml#EXIT-04 — owner notification dead-lettered after 5 attempts.
    NOTIFICATION_DEAD_LETTER = "NOTIFICATION_DEAD_LETTER"


class AdminTaskStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class EventType(StrEnum):
    """Kafka event types emitted by this module.

    One entry only: the sole event the kit calls for is the owner notification
    (rules.yaml#EXIT-04, states.yaml side effect ``notify_owner``).
    """

    #: states.yaml side_effect ``notify_owner`` on DOCS_SUBMITTED -> OWNER_NOTIFIED.
    EXIT_INITIATED_OWNER_NOTIFICATION = "exit_workflow.owner_notification_requested"
