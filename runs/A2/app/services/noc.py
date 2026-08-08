"""Exit NOC issuance, download and verification (SRS T13 step 10, O16)."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.core.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
)
from app.core.security import generate_verification_code
from app.domain import events as ev
from app.domain.enums import ActorRole, ExitWorkflowState, NocStatus
from app.domain.events import DomainEvent
from app.models.exit_workflow import ExitWorkflow
from app.models.noc import ExitNoc
from app.ports.noc_renderer import NocDeductionLine, NocFacts, NocRenderer
from app.ports.notifications import NotificationTemplate
from app.ports.storage import DocumentNotStoredError, DocumentStorage
from app.repositories.support import InspectionRepository, NocRepository, SettlementRepository
from app.schemas.noc import NocVerificationResponse
from app.services.notifications import NotificationService, base_context, tenant_recipient
from app.services.workflow_engine import WorkflowEngine


class NocService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        engine: WorkflowEngine,
        notifications: NotificationService,
        storage: DocumentStorage,
        renderer: NocRenderer,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._engine = engine
        self._notifications = notifications
        self._storage = storage
        self._renderer = renderer
        self._repo = NocRepository(session)
        self._settlements = SettlementRepository(session)
        self._inspections = InspectionRepository(session)

    # --------------------------------------------------------- issuance
    async def issue(self, workflow: ExitWorkflow, *, ctx: RequestContext) -> ExitNoc:
        """Generate and store the certificate. Idempotent per workflow."""
        existing = await self._repo.get_for_workflow(workflow.id)
        if existing is not None:
            return existing

        if workflow.state is not ExitWorkflowState.SETTLEMENT_COMPLETED:
            raise ConflictError(
                "The Exit NOC is issued once the deposit settlement has been paid.",
                code="noc_not_available",
                details={"state": workflow.state.value},
            )

        settlement = await self._settlements.require_for_workflow(workflow.id)
        inspection = await self._inspections.get_for_workflow(workflow.id)

        now = self._clock.now()
        noc_id = uuid.uuid4()
        noc_number = await self._repo.next_number(now.year)
        verification_code = generate_verification_code()
        verification_url = (
            f"{self._settings.noc_verification_base_url.rstrip('/')}/{verification_code}"
        )

        property_snapshot = workflow.property_snapshot or {}
        tenant_snapshot = workflow.tenant_snapshot or {}
        owner_snapshot = workflow.owner_snapshot or {}

        facts = NocFacts(
            noc_number=noc_number,
            workflow_reference=workflow.reference or str(workflow.id),
            issued_at=now,
            effective_date=workflow.move_out_date or now.date(),
            issuer_name=self._settings.noc_issuer_name,
            issuer_address=self._settings.noc_issuer_address,
            tenant_name=tenant_snapshot.get("name") or "Tenant",
            tenant_identifier=tenant_snapshot.get("identifier"),
            owner_name=owner_snapshot.get("name") or "Owner",
            property_reference=property_snapshot.get("reference") or str(workflow.property_id),
            property_address=property_snapshot.get("address") or "-",
            contract_reference=str(workflow.contract_id),
            move_out_date=workflow.move_out_date or now.date(),
            inspection_date=(
                inspection.conducted_at.date()
                if inspection and inspection.conducted_at
                else None
            ),
            inspection_agency=inspection.agency_name if inspection else None,
            currency=settlement.currency,
            deposit_amount=settlement.deposit_amount,
            total_deductions=settlement.total_deductions,
            net_refund_amount=settlement.net_refund_amount,
            tenant_liability_amount=settlement.tenant_liability_amount,
            deduction_lines=tuple(
                NocDeductionLine(
                    description=line.description,
                    category=line.category.value,
                    amount=line.amount,
                )
                for line in settlement.deductions
            ),
            settlement_paid_at=settlement.payment_completed_at,
            payment_reference=settlement.payment_reference,
            verification_code=verification_code,
            verification_url=verification_url,
            notes=(
                (
                    "A balance of "
                    f"{settlement.currency} {settlement.tenant_liability_amount} remains "
                    "recoverable from the tenant and is not discharged by this "
                    "certificate.",
                )
                if settlement.tenant_liability_amount > 0
                else ()
            ),
        )

        pdf = await self._renderer.render(facts)
        storage_key = f"exit-workflows/{workflow.id}/noc/{noc_id}/{noc_number}.pdf"
        stored = await self._storage.put(
            key=storage_key,
            data=pdf,
            content_type="application/pdf",
            metadata={"workflow_id": str(workflow.id), "noc_number": noc_number},
        )

        noc = ExitNoc(
            id=noc_id,
            workflow_id=workflow.id,
            noc_number=noc_number,
            status=NocStatus.ISSUED,
            issued_at=now,
            issued_by=ctx.principal.actor_id,
            effective_date=facts.effective_date,
            storage_key=stored.storage_key,
            content_type="application/pdf",
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
            verification_code=verification_code,
            verification_url=verification_url,
            rendered_facts=facts.as_dict(),
        )
        self._repo.add(noc)

        self._engine.transition(
            workflow,
            action="issue_noc",
            ctx=ctx,
            event_type=ev.NOC_ISSUED,
            event_payload={
                "noc_id": str(noc.id),
                "noc_number": noc_number,
                "checksum_sha256": stored.checksum_sha256,
                "net_refund_amount": str(settlement.net_refund_amount),
            },
            audit_changes={"noc_number": noc_number},
        )
        self._notifications.enqueue(
            template=NotificationTemplate.TENANT_NOC_ISSUED,
            recipients=(tenant_recipient(workflow),),
            context={
                **base_context(workflow),
                "noc_number": noc_number,
                "verification_code": verification_code,
                "verification_url": verification_url,
            },
            dedupe_key=f"{noc.id}:issued",
        )
        return noc

    # --------------------------------------------------------- download
    async def get(self, workflow: ExitWorkflow) -> ExitNoc:
        return await self._repo.require_for_workflow(workflow.id)

    async def download(
        self, workflow: ExitWorkflow, ctx: RequestContext
    ) -> tuple[ExitNoc, bytes]:
        """SRS T13 step 10: "digital NOC download"."""
        noc = await self._repo.require_for_workflow(workflow.id)
        if noc.status is NocStatus.REVOKED:
            raise ConflictError(
                "This Exit NOC has been revoked and can no longer be downloaded.",
                code="noc_revoked",
                details={"revoked_at": noc.revoked_at.isoformat() if noc.revoked_at else None},
            )
        try:
            data = await self._storage.get(noc.storage_key)
        except DocumentNotStoredError as exc:
            raise NotFoundError("The stored NOC could not be located.") from exc

        if hashlib.sha256(data).hexdigest() != noc.checksum_sha256:
            raise ConflictError(
                "The stored NOC failed its integrity check and was not served.",
                code="noc_integrity_failure",
                details={"noc_id": str(noc.id)},
            )

        await self._repo.record_download(noc.id, self._clock.now())
        self._engine.audit(
            ctx,
            action="download_noc",
            entity_type="noc",
            entity_id=noc.id,
            workflow=workflow,
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.NOC_DOWNLOADED,
                workflow_id=workflow.id,
                payload={"noc_id": str(noc.id), "noc_number": noc.noc_number},
            ),
            ctx,
        )
        return noc, data

    # ------------------------------------------------------ verification
    async def verify(self, code: str) -> NocVerificationResponse:
        """Public check of a certificate's authenticity.

        Returns the same shape whether or not the code exists, so the endpoint cannot be
        used to enumerate issued certificates.
        """
        noc = await self._repo.get_by_verification_code(code.strip().upper())
        if noc is None:
            return NocVerificationResponse(
                valid=False, message="No certificate matches this verification code."
            )
        if noc.status is NocStatus.REVOKED:
            return NocVerificationResponse(
                valid=False,
                noc_number=noc.noc_number,
                status=noc.status,
                issued_at=noc.issued_at,
                effective_date=noc.effective_date,
                revoked_at=noc.revoked_at,
                message="This certificate has been revoked.",
            )
        facts = noc.rendered_facts or {}
        return NocVerificationResponse(
            valid=True,
            noc_number=noc.noc_number,
            status=noc.status,
            issued_at=noc.issued_at,
            effective_date=noc.effective_date,
            property_reference=facts.get("property_reference"),
            message="This certificate is valid.",
        )

    async def revoke(
        self, workflow: ExitWorkflow, reason: str, ctx: RequestContext
    ) -> ExitNoc:
        """Withdraw a certificate issued in error. ADMIN only, and never silent."""
        if ctx.principal.role is not ActorRole.ADMIN:
            raise AuthorizationError("Only an administrator may revoke an Exit NOC.")

        noc = await self._repo.require_for_workflow(workflow.id)
        if noc.status is NocStatus.REVOKED:
            return noc

        noc.status = NocStatus.REVOKED
        noc.revoked_at = self._clock.now()
        noc.revoked_by = ctx.principal.actor_id
        noc.revocation_reason = reason

        self._engine.audit(
            ctx,
            action="revoke_noc",
            entity_type="noc",
            entity_id=noc.id,
            workflow=workflow,
            changes={"status": {"from": "ISSUED", "to": "REVOKED"}},
            context={"reason": reason},
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.NOC_REVOKED,
                workflow_id=workflow.id,
                payload={"noc_id": str(noc.id), "reason": reason},
            ),
            ctx,
        )
        return noc
