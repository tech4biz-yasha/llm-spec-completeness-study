"""Notification port.

SRS O15 requires an email to the registered inspection agency carrying the property
details, and T13 step 6 requires owner notification on submission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class NotificationChannel(StrEnum):
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    SMS = "SMS"
    IN_APP = "IN_APP"


class NotificationTemplate(StrEnum):
    OWNER_EXIT_SUBMITTED = "OWNER_EXIT_SUBMITTED"
    TENANT_EXIT_APPROVED = "TENANT_EXIT_APPROVED"
    TENANT_EXIT_REJECTED = "TENANT_EXIT_REJECTED"
    AGENCY_INSPECTION_REQUESTED = "AGENCY_INSPECTION_REQUESTED"
    PARTIES_INSPECTION_SLOTS_AVAILABLE = "PARTIES_INSPECTION_SLOTS_AVAILABLE"
    PARTIES_INSPECTION_SCHEDULED = "PARTIES_INSPECTION_SCHEDULED"
    PARTIES_INSPECTION_REPORT_READY = "PARTIES_INSPECTION_REPORT_READY"
    TENANT_DAMAGE_REVIEW_OPENED = "TENANT_DAMAGE_REVIEW_OPENED"
    OWNER_DISPUTE_RAISED = "OWNER_DISPUTE_RAISED"
    TENANT_DISPUTE_RESOLVED = "TENANT_DISPUTE_RESOLVED"
    OWNER_SETTLEMENT_READY = "OWNER_SETTLEMENT_READY"
    TENANT_REFUND_PAID = "TENANT_REFUND_PAID"
    OWNER_REFUND_FAILED = "OWNER_REFUND_FAILED"
    TENANT_NOC_ISSUED = "TENANT_NOC_ISSUED"
    PARTIES_EXIT_COMPLETED = "PARTIES_EXIT_COMPLETED"


@dataclass(frozen=True, slots=True)
class Recipient:
    actor_id: str | None
    email: str | None = None
    phone: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Notification:
    template: NotificationTemplate
    channels: tuple[NotificationChannel, ...]
    recipients: tuple[Recipient, ...]
    context: dict[str, Any] = field(default_factory=dict)
    #: Deduplication key so an at-least-once event relay cannot email twice.
    dedupe_key: str | None = None


class Notifier(Protocol):
    async def send(self, notification: Notification) -> None: ...
