"""Outbound notifications.

Messages are *queued* inside the business transaction and delivered by the
background worker, so nothing is emailed about a change that did not commit.
Appendix B requires the agency email to carry the property details and the
workflow id; :func:`render` builds those bodies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.logging import get_logger
from exit_workflow.core.serialization import jsonable
from exit_workflow.domain.enums import NotificationChannel
from exit_workflow.models.notification import NotificationLog

log = get_logger(__name__)


class Template:
    OWNER_EXIT_REQUESTED = "owner.exit_requested"
    TENANT_EXIT_APPROVED = "tenant.exit_approved"
    TENANT_EXIT_REJECTED = "tenant.exit_rejected"
    AGENCY_INSPECTION_REQUESTED = "agency.inspection_requested"
    PARTIES_SLOTS_PROPOSED = "parties.inspection_slots_proposed"
    PARTIES_INSPECTION_SCHEDULED = "parties.inspection_scheduled"
    PARTIES_DAMAGE_REPORT_READY = "parties.damage_report_ready"
    OWNER_DAMAGE_DISPUTED = "owner.damage_disputed"
    OWNER_SETTLEMENT_READY = "owner.settlement_ready"
    TENANT_SETTLEMENT_PAID = "tenant.settlement_paid"
    TENANT_NOC_READY = "tenant.noc_ready"


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    subject: str
    body: str


def _lines(*parts: str | None) -> str:
    return "\n".join(p for p in parts if p is not None)


def render(template: str, ctx: dict[str, Any]) -> RenderedMessage:
    ref = ctx.get("reference", "")
    prop = ctx.get("property_address") or ctx.get("property_reference") or "the property"

    match template:
        case Template.OWNER_EXIT_REQUESTED:
            return RenderedMessage(
                subject=f"[{ref}] Exit request from {ctx.get('tenant_name', 'your tenant')}",
                body=_lines(
                    f"Dear {ctx.get('owner_name', 'Owner')},",
                    "",
                    f"{ctx.get('tenant_name', 'Your tenant')} has requested to exit {prop}.",
                    "",
                    f"Workflow ID   : {ref}",
                    f"Move-out date : {ctx.get('move_out_date')}",
                    f"Reason        : {ctx.get('reason')}",
                    f"Documents     : {ctx.get('document_count', 0)} attached",
                    "",
                    "Review and approve the exit in the Owner Portal to start the "
                    "inspection process.",
                ),
            )
        case Template.TENANT_EXIT_APPROVED:
            return RenderedMessage(
                subject=f"[{ref}] Your exit request was approved",
                body=_lines(
                    f"Dear {ctx.get('tenant_name', 'Tenant')},",
                    "",
                    f"Your request to exit {prop} on {ctx.get('move_out_date')} has been "
                    "approved.",
                    "An inspection will be arranged with a registered inspection agency; "
                    "you will be asked to confirm an appointment date.",
                    "",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.TENANT_EXIT_REJECTED:
            return RenderedMessage(
                subject=f"[{ref}] Your exit request was not approved",
                body=_lines(
                    f"Dear {ctx.get('tenant_name', 'Tenant')},",
                    "",
                    f"Your request to exit {prop} was not approved.",
                    f"Reason: {ctx.get('rejection_reason', 'not provided')}",
                    "",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.AGENCY_INSPECTION_REQUESTED:
            return RenderedMessage(
                subject=f"[{ref}] Move-out inspection request — {prop}",
                body=_lines(
                    f"Dear {ctx.get('agency_name', 'Partner')},",
                    "",
                    "You have received a move-out inspection request via Meridian.",
                    "",
                    f"Workflow ID     : {ref}",
                    f"Inspection ref  : {ctx.get('inspection_reference')}",
                    f"Property        : {prop}",
                    f"Property ref    : {ctx.get('property_reference')}",
                    f"Move-out date   : {ctx.get('move_out_date')}",
                    f"Tenant          : {ctx.get('tenant_name')}",
                    f"Owner           : {ctx.get('owner_name')}",
                    f"Notes           : {ctx.get('request_notes') or '-'}",
                    "",
                    "Please respond with your available inspection dates through the "
                    "agency portal.",
                ),
            )
        case Template.PARTIES_SLOTS_PROPOSED:
            slots = ctx.get("slots") or []
            return RenderedMessage(
                subject=f"[{ref}] Inspection dates available for selection",
                body=_lines(
                    f"{ctx.get('agency_name', 'The inspection agency')} has proposed "
                    f"{len(slots)} appointment window(s) for {prop}:",
                    "",
                    *[f"  - {s}" for s in slots],
                    "",
                    "Select a slot in the app to confirm the inspection.",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.PARTIES_INSPECTION_SCHEDULED:
            return RenderedMessage(
                subject=f"[{ref}] Move-out inspection confirmed",
                body=_lines(
                    f"The move-out inspection for {prop} is confirmed.",
                    "",
                    f"Starts : {ctx.get('scheduled_start')}",
                    f"Ends   : {ctx.get('scheduled_end')}",
                    f"Agency : {ctx.get('agency_name')}",
                    "",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.PARTIES_DAMAGE_REPORT_READY:
            return RenderedMessage(
                subject=f"[{ref}] Inspection report available for review",
                body=_lines(
                    f"The inspection report for {prop} has been uploaded.",
                    "",
                    f"Assessed damage : {ctx.get('currency')} {ctx.get('assessed_total')}",
                    f"Line items      : {ctx.get('line_item_count')}",
                    f"Deposit held    : {ctx.get('currency')} {ctx.get('security_deposit_amount')}",
                    "",
                    "Review the report in the app. The deposit settlement follows once the "
                    "owner finalises the deduction.",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.OWNER_DAMAGE_DISPUTED:
            return RenderedMessage(
                subject=f"[{ref}] Tenant disputed the inspection report",
                body=_lines(
                    f"Dear {ctx.get('owner_name', 'Owner')},",
                    "",
                    f"The tenant has disputed the damage assessment for {prop}.",
                    f"Reason: {ctx.get('dispute_reason')}",
                    "",
                    "Settlement is on hold until the dispute is resolved.",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.OWNER_SETTLEMENT_READY:
            return RenderedMessage(
                subject=f"[{ref}] Deposit settlement ready to pay",
                body=_lines(
                    f"Dear {ctx.get('owner_name', 'Owner')},",
                    "",
                    f"The deposit settlement for {prop} is ready.",
                    "",
                    f"Security deposit : {ctx.get('currency')} "
                    f"{ctx.get('security_deposit_amount')}",
                    f"Damage deduction : {ctx.get('currency')} {ctx.get('total_deduction_amount')}",
                    f"Refund to tenant : {ctx.get('currency')} {ctx.get('refund_amount')}",
                    "",
                    "Use 'Pay Deposit' in the Owner Portal to release the refund. The exit "
                    "NOC is generated automatically once payment succeeds.",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.TENANT_SETTLEMENT_PAID:
            return RenderedMessage(
                subject=f"[{ref}] Your deposit refund has been released",
                body=_lines(
                    f"Dear {ctx.get('tenant_name', 'Tenant')},",
                    "",
                    f"Your deposit refund of {ctx.get('currency')} {ctx.get('refund_amount')} "
                    f"for {prop} has been released.",
                    f"Payment reference: {ctx.get('payment_reference')}",
                    "",
                    f"Workflow ID: {ref}",
                ),
            )
        case Template.TENANT_NOC_READY:
            return RenderedMessage(
                subject=f"[{ref}] Your Exit NOC is ready to download",
                body=_lines(
                    f"Dear {ctx.get('tenant_name', 'Tenant')},",
                    "",
                    f"Your Exit No Objection Certificate for {prop} has been issued.",
                    "",
                    f"NOC number        : {ctx.get('noc_number')}",
                    f"Verification code : {ctx.get('verification_code')}",
                    "",
                    "Download it from the Exit section of the app. Your exit workflow is "
                    "now complete.",
                    f"Workflow ID: {ref}",
                ),
            )
        case _:  # pragma: no cover - defensive
            raise ValueError(f"Unknown notification template {template!r}")


class EmailSender(Protocol):
    async def send(self, recipient: str, subject: str, body: str) -> str: ...


class LoggingEmailSender:
    """Default sender. Bind an SES/SMTP adapter in production."""

    async def send(self, recipient: str, subject: str, body: str) -> str:
        reference = uuid.uuid4().hex
        log.info(
            "notification_sent",
            recipient=recipient,
            subject=subject,
            provider_reference=reference,
            body_preview=body[:200],
        )
        return reference


class NotificationService:
    """Queues messages; the background worker delivers them."""

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def enqueue(
        self,
        *,
        template: str,
        recipient: str | None,
        context: dict[str, Any],
        workflow_id: uuid.UUID | None = None,
        channel: NotificationChannel = NotificationChannel.EMAIL,
    ) -> NotificationLog | None:
        """Queue one message. A missing recipient is skipped, not fatal.

        Contact details are snapshots from the Property service; an exit must
        not fail because one of them is absent.
        """

        if not recipient:
            log.warning("notification_skipped_no_recipient", template=template)
            return None
        message = render(template, context)
        now = utcnow()
        row = NotificationLog(
            workflow_id=workflow_id,
            channel=channel,
            template=template,
            recipient=recipient,
            subject=message.subject[:255],
            body=message.body,
            context=jsonable(context),
            available_at=now,
        )
        self._session.add(row)
        return row

    def enqueue_many(
        self,
        *,
        template: str,
        recipients: list[str | None],
        context: dict[str, Any],
        workflow_id: uuid.UUID | None = None,
    ) -> list[NotificationLog]:
        seen: set[str] = set()
        rows: list[NotificationLog] = []
        for recipient in recipients:
            if not recipient or recipient in seen:
                continue
            seen.add(recipient)
            if row := self.enqueue(
                template=template,
                recipient=recipient,
                context=context,
                workflow_id=workflow_id,
            ):
                rows.append(row)
        return rows
