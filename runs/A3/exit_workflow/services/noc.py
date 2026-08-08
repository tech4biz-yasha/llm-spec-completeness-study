"""O16 / T13 steps 9-10 — Exit NOC issuance, download and verification."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.errors import ConflictError, NotFoundError
from exit_workflow.core.ids import noc_number, verification_code
from exit_workflow.core.money import format_amount
from exit_workflow.domain.enums import ExitWorkflowStatus
from exit_workflow.models.noc import ExitNoc
from exit_workflow.models.settlement import Settlement
from exit_workflow.models.workflow import ExitWorkflow
from exit_workflow.services import access
from exit_workflow.services.audit import AuditRecorder
from exit_workflow.services.context import ServiceContext
from exit_workflow.services.events import AggregateType, EventRecorder, EventType
from exit_workflow.services.notifications import NotificationService, Template
from exit_workflow.services.pdf import Line, render_pdf
from exit_workflow.services.storage import DocumentStorage, sha256_hex
from exit_workflow.services.transitions import apply_transition

_MAX_NUMBER_ATTEMPTS = 5


@dataclass(frozen=True, slots=True)
class NocVerification:
    noc_number: str
    valid: bool
    issued_at: str
    property_reference: str | None
    move_out_date: str
    revoked: bool
    revocation_reason: str | None


class NocService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        ctx: ServiceContext,
        *,
        storage: DocumentStorage,
        audit: AuditRecorder,
        events: EventRecorder,
        notifications: NotificationService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._ctx = ctx
        self._storage = storage
        self._audit = audit
        self._events = events
        self._notifications = notifications

    # -- issuance ----------------------------------------------------------
    async def issue(self, workflow: ExitWorkflow, settlement: Settlement) -> ExitNoc:
        """Auto-generate the NOC after a successful payment, then complete.

        Idempotent: if a certificate already exists for the workflow it is
        returned unchanged, so a reconciliation replay cannot mint a second
        certificate for the same exit.
        """

        existing = (
            await self._session.execute(select(ExitNoc).where(ExitNoc.workflow_id == workflow.id))
        ).scalars().first()
        if existing is not None:
            return existing

        if workflow.status is not ExitWorkflowStatus.SETTLEMENT_COMPLETED:
            raise ConflictError(
                f"An Exit NOC can only be issued once the deposit is settled (workflow is "
                f"{workflow.status.value})."
            )

        now = utcnow()
        noc = ExitNoc(
            workflow_id=workflow.id,
            settlement_id=settlement.id,
            noc_number=noc_number(self._settings.noc_number_prefix, now.year),
            verification_code=verification_code(),
            issued_at=now,
            property_id=workflow.property_id,
            property_reference=workflow.property_reference,
            property_address=workflow.property_address,
            tenant_id=workflow.tenant_id,
            tenant_name=workflow.tenant_name,
            owner_id=workflow.owner_id,
            owner_name=workflow.owner_name,
            contract_id=workflow.contract_id,
            move_out_date=workflow.move_out_date,
            currency=settlement.currency,
            security_deposit_amount=settlement.security_deposit_amount,
            total_deduction_amount=settlement.total_deduction_amount,
            refund_amount=settlement.refund_amount,
            storage_key="",  # set below, once the number is final
            content_sha256="0" * 64,
            size_bytes=1,
        )

        for attempt in range(_MAX_NUMBER_ATTEMPTS):
            self._session.add(noc)
            try:
                async with self._session.begin_nested():
                    await self._session.flush()
            except IntegrityError:
                if attempt == _MAX_NUMBER_ATTEMPTS - 1:  # pragma: no cover - improbable
                    raise
                self._session.expunge(noc)
                noc.noc_number = noc_number(self._settings.noc_number_prefix, now.year)
                noc.verification_code = verification_code()
                continue
            break

        pdf = render_pdf(
            f"Exit NOC {noc.noc_number}",
            self._certificate_lines(workflow, settlement, noc),
            created_at=now,
        )
        key = f"exit-noc/{workflow.id}/{noc.noc_number}.pdf"
        await self._storage.put(key, pdf, "application/pdf")

        noc.storage_key = key
        noc.content_sha256 = sha256_hex(pdf)
        noc.size_bytes = len(pdf)

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.NOC_ISSUED,
            reason=f"Exit NOC {noc.noc_number} generated",
            system=True,
            attributes={"noc_number": noc.noc_number},
        )
        workflow.noc_issued_at = now

        # T13 step 10. Completion is not deferred until the tenant downloads
        # the certificate: BR-1 would otherwise keep both parties locked out of
        # new contracts indefinitely.
        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.COMPLETED,
            reason="Exit workflow completed",
            system=True,
        )

        self._audit.record(
            self._ctx,
            action="noc.issued",
            entity_type="exit_noc",
            entity_id=noc.id,
            workflow_id=workflow.id,
            changes={
                "noc_number": noc.noc_number,
                "content_sha256": noc.content_sha256,
                "refund_amount": settlement.refund_amount,
            },
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.NOC_ISSUED,
            aggregate_type=AggregateType.NOC,
            aggregate_id=noc.id,
            workflow_id=workflow.id,
            payload={
                "noc_id": noc.id,
                "noc_number": noc.noc_number,
                "tenant_id": workflow.tenant_id,
                "property_id": workflow.property_id,
                "content_sha256": noc.content_sha256,
            },
        )
        self._notifications.enqueue(
            template=Template.TENANT_NOC_READY,
            recipient=workflow.tenant_email,
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "tenant_name": workflow.tenant_name,
                "property_address": workflow.property_address,
                "noc_number": noc.noc_number,
                "verification_code": noc.verification_code,
            },
        )
        return noc

    def _certificate_lines(
        self, workflow: ExitWorkflow, settlement: Settlement, noc: ExitNoc
    ) -> list[Line]:
        cur = settlement.currency
        return [
            Line("MERIDIAN", size=10, bold=True, space_after=2),
            Line("EXIT NO OBJECTION CERTIFICATE", size=18, bold=True, space_after=4),
            Line(rule=True, space_after=8),
            Line(f"NOC number       : {noc.noc_number}", size=11),
            Line(f"Workflow ID      : {workflow.reference}", size=11),
            Line(f"Issued           : {noc.issued_at.strftime('%d %B %Y %H:%M UTC')}", size=11),
            Line(rule=True, space_after=8),
            Line("PROPERTY", size=10, bold=True),
            Line(f"Reference : {workflow.property_reference or '-'}", size=11),
            Line(f"Address   : {workflow.property_address or '-'}", size=11),
            Line("", space_after=2),
            Line("PARTIES", size=10, bold=True),
            Line(f"Tenant : {workflow.tenant_name or workflow.tenant_id}", size=11),
            Line(f"Owner  : {workflow.owner_name or workflow.owner_id}", size=11),
            Line(f"Contract : {workflow.contract_id}", size=11),
            Line(f"Move-out date : {workflow.move_out_date.isoformat()}", size=11),
            Line(rule=True, space_after=8),
            Line("DEPOSIT SETTLEMENT", size=10, bold=True),
            Line(
                f"Security deposit held : "
                f"{format_amount(settlement.security_deposit_amount, cur)}",
                size=11,
            ),
            Line(
                f"Damage deduction      : "
                f"{format_amount(settlement.total_deduction_amount, cur)}",
                size=11,
            ),
            Line(
                f"Refunded to tenant    : {format_amount(settlement.refund_amount, cur)}",
                size=11,
                bold=True,
            ),
            *(
                [
                    Line(
                        f"Balance due from tenant: "
                        f"{format_amount(settlement.balance_due_from_tenant, cur)}",
                        size=11,
                    )
                ]
                if settlement.balance_due_from_tenant > 0
                else []
            ),
            Line(f"Payment reference     : {settlement.payment_reference or '-'}", size=11),
            Line(rule=True, space_after=8),
            Line(
                "The owner confirms no objection to the tenant vacating the property named "
                "above.",
                size=10,
            ),
            Line(
                "The deposit settlement recorded here is final and the exit workflow is "
                "complete.",
                size=10,
            ),
            Line("", space_after=6),
            Line(f"Verification code : {noc.verification_code}", size=10, bold=True),
            Line(
                "Verify at https://meridian.ae/verify-noc using the NOC number and code above.",
                size=9,
            ),
            Line(
                "This is a system-generated certificate and is valid without a signature.",
                size=9,
            ),
        ]

    # -- read / download ---------------------------------------------------
    async def get(self, workflow: ExitWorkflow) -> ExitNoc:
        access.ensure_can_view(workflow, self._ctx.require_principal())
        noc = (
            await self._session.execute(select(ExitNoc).where(ExitNoc.workflow_id == workflow.id))
        ).scalars().first()
        if noc is None:
            raise NotFoundError("No Exit NOC has been issued for this workflow yet.")
        return noc

    async def download(self, workflow: ExitWorkflow) -> tuple[ExitNoc, bytes]:
        noc = await self.get(workflow)
        if noc.revoked_at is not None:
            raise ConflictError(
                "This Exit NOC has been revoked.",
                extra={"revocation_reason": noc.revocation_reason},
            )
        data = await self._storage.get(noc.storage_key)
        if sha256_hex(data) != noc.content_sha256:
            raise ConflictError(
                "The stored Exit NOC failed its integrity check and cannot be served.",
                extra={"noc_number": noc.noc_number},
            )

        now = utcnow()
        noc.download_count += 1
        noc.last_downloaded_at = now
        if noc.first_downloaded_at is None:
            noc.first_downloaded_at = now
        if workflow.noc_first_downloaded_at is None:
            workflow.noc_first_downloaded_at = now

        self._audit.record(
            self._ctx,
            action="noc.downloaded",
            entity_type="exit_noc",
            entity_id=noc.id,
            workflow_id=workflow.id,
            changes={"noc_number": noc.noc_number, "download_count": noc.download_count},
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.NOC_DOWNLOADED,
            aggregate_type=AggregateType.NOC,
            aggregate_id=noc.id,
            workflow_id=workflow.id,
            payload={"noc_id": noc.id, "noc_number": noc.noc_number},
        )
        return noc, data

    async def verify(self, number: str, code: str) -> NocVerification:
        """Third-party check. The code is required, so this is not an oracle."""

        noc = (
            await self._session.execute(
                select(ExitNoc).where(
                    ExitNoc.noc_number == number.strip().upper(),
                    ExitNoc.verification_code == code.strip().upper(),
                )
            )
        ).scalars().first()
        if noc is None:
            raise NotFoundError("No Exit NOC matches that number and verification code.")
        return NocVerification(
            noc_number=noc.noc_number,
            valid=noc.is_valid,
            issued_at=noc.issued_at.isoformat(),
            property_reference=noc.property_reference,
            move_out_date=noc.move_out_date.isoformat(),
            revoked=noc.revoked_at is not None,
            revocation_reason=noc.revocation_reason,
        )

    async def revoke(self, workflow: ExitWorkflow, *, reason: str) -> ExitNoc:
        """Administrative correction path (e.g. a certificate issued in error)."""

        principal = self._ctx.require_principal()
        if not principal.is_admin:
            raise ConflictError("Only an administrator may revoke an Exit NOC.")
        noc = await self.get(workflow)
        if noc.revoked_at is not None:
            raise ConflictError("This Exit NOC is already revoked.")
        noc.revoked_at = utcnow()
        noc.revoked_by = principal.subject_id
        noc.revocation_reason = reason
        self._audit.record(
            self._ctx,
            action="noc.revoked",
            entity_type="exit_noc",
            entity_id=noc.id,
            workflow_id=workflow.id,
            changes={"reason": reason},
        )
        return noc
