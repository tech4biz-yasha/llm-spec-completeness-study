"""Outbound notifications (SRS O15 verification lists Email)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class NotificationTemplate(StrEnum):
    OWNER_EXIT_REQUESTED = "owner.exit_requested"
    TENANT_EXIT_APPROVED = "tenant.exit_approved"
    TENANT_EXIT_REJECTED = "tenant.exit_rejected"
    #: Appendix B: "email sent to registered inspection agency with property details".
    AGENCY_INSPECTION_REQUESTED = "agency.inspection_requested"
    PARTIES_SLOTS_PROPOSED = "parties.inspection_slots_proposed"
    PARTIES_INSPECTION_SCHEDULED = "parties.inspection_scheduled"
    PARTIES_DAMAGE_REPORT_READY = "parties.damage_report_ready"
    OWNER_SETTLEMENT_PAYABLE = "owner.settlement_payable"
    TENANT_BALANCE_DUE = "tenant.balance_due"
    TENANT_REFUND_PAID = "tenant.refund_paid"
    TENANT_NOC_READY = "tenant.noc_ready"
    PARTIES_WORKFLOW_COMPLETED = "parties.workflow_completed"


class Channel(StrEnum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    PUSH = "PUSH"


@dataclass(frozen=True, slots=True)
class Notification:
    template: NotificationTemplate
    channel: Channel
    recipient: str
    context: dict[str, Any] = field(default_factory=dict)
    subject: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "template": self.template.value,
            "channel": self.channel.value,
            "recipient": self.recipient,
            "subject": self.subject,
            "context": self.context,
        }


@runtime_checkable
class NotificationPort(Protocol):
    async def send(self, notification: Notification) -> None:
        """Deliver one notification. Must raise on failure so the relay retries."""


class LoggingNotifier:
    """Default adapter: emits a structured log line instead of contacting a provider.

    Real deployments swap this for an SES/SendGrid adapter at wiring time; nothing in the
    services changes.
    """

    async def send(self, notification: Notification) -> None:
        logger.info(
            "notification dispatched",
            extra={
                "template": notification.template.value,
                "channel": notification.channel.value,
                "recipient": notification.recipient,
                "subject": notification.subject,
            },
        )


class NullNotifier:
    """Discards notifications. Used when ``NOTIFICATIONS_ENABLED=false``."""

    async def send(self, notification: Notification) -> None:
        return None
