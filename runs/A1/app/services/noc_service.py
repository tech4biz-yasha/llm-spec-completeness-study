"""Digital Exit NOC issuance and download (SRS T13 step 10, O16).

The certificate is generated automatically the moment the settlement closes, rendered to a
real PDF, hashed, and stored immutably. Downloading it completes the workflow, which releases
the BR-1 contract lock.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

import sqlalchemy as sa

from app.domain import pdf
from app.domain.states import ExitWorkflowState
from app.errors import ConflictError, NotFoundError
from app.models.audit import AuditAction
from app.models.base import utcnow
from app.models.inspection import DamageReport
from app.models.noc import ExitNOC
from app.models.settlement import DepositSettlement, SettlementStatus
from app.models.workflow import ExitWorkflow
from app.money import format_aed
from app.ports.events import EventType
from app.ports.notifications import Channel, Notification, NotificationTemplate
from app.services.base import ServiceBase
from app.services.references import next_noc_number
from app.services.workflow_service import WorkflowService

S = ExitWorkflowState


class NOCService(ServiceBase):
    def _workflow_service(self) -> WorkflowService:
        return WorkflowService(
            self.session, self.ctx, self.settings, recorder=self.events, audit=self.audit
        )

    async def issue(self, workflow: ExitWorkflow, settlement: DepositSettlement) -> ExitNOC:
        """Generate and store the Exit NOC. Called automatically when a settlement closes."""
        if settlement.status is not SettlementStatus.CLOSED:
            raise ConflictError(
                "the NOC can only be issued once the settlement is closed",
                details={"settlement_status": settlement.status.value},
            )
        if workflow.noc is not None:
            return workflow.noc

        report = None
        if settlement.damage_report_id is not None:
            report = await self.session.scalar(
                sa.select(DamageReport).where(DamageReport.id == settlement.damage_report_id)
            )
            if report is not None:
                await self.session.refresh(report, ["line_items"])

        noc_number = await next_noc_number(self.session)
        issued_at = utcnow()
        snapshot = _build_snapshot(workflow, settlement, noc_number, issued_at)
        document = render_noc_pdf(workflow, settlement, report, noc_number, issued_at)
        digest = hashlib.sha256(document).hexdigest()

        noc = ExitNOC(
            workflow_id=workflow.id,
            settlement_id=settlement.id,
            noc_number=noc_number,
            issued_at=issued_at,
            pdf_bytes=document,
            content_sha256=digest,
            byte_size=len(document),
            snapshot=snapshot,
        )
        self.session.add(noc)
        workflow.noc = noc

        workflows = self._workflow_service()
        workflows.transition(
            workflow, S.NOC_ISSUED, note=f"exit NOC {noc_number} issued", context={"noc_number": noc_number}
        )
        workflow.noc_issued_at = issued_at
        await self.session.flush()

        self.audit.record(
            AuditAction.NOC_ISSUED,
            entity_type="ExitNOC",
            entity_id=noc.id,
            workflow_id=workflow.id,
            payload={
                "noc_number": noc_number,
                "content_sha256": digest,
                "byte_size": len(document),
            },
        )
        workflows.emit(
            workflow,
            EventType.NOC_ISSUED,
            {"noc_number": noc_number, "content_sha256": digest},
        )
        self.notify(
            workflow,
            Notification(
                template=NotificationTemplate.TENANT_NOC_READY,
                channel=Channel.EMAIL,
                recipient=workflow.tenant.email,
                subject=f"Your Exit NOC {noc_number} is ready",
                context={
                    "reference": workflow.reference,
                    "noc_number": noc_number,
                    "refund": format_aed(settlement.refund_fils),
                },
            ),
        )
        return noc

    async def get(self, workflow_id: uuid.UUID) -> ExitNOC:
        workflow = await self.load_workflow(workflow_id)
        self.authorize_participant(workflow)
        if workflow.noc is None:
            raise NotFoundError(
                "no NOC has been issued for this workflow",
                details={"workflow_id": str(workflow_id), "state": workflow.state.value},
            )
        return workflow.noc

    async def download(self, workflow_id: uuid.UUID) -> ExitNOC:
        """Return the NOC for download and complete the workflow (T13 step 10).

        Completion is idempotent: subsequent downloads only bump the counters.
        """
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow)
        if workflow.noc is None:
            raise NotFoundError(
                "no NOC has been issued for this workflow",
                details={"workflow_id": str(workflow_id), "state": workflow.state.value},
            )

        noc = workflow.noc
        now = utcnow()
        noc.download_count += 1
        noc.last_downloaded_at = now
        if noc.first_downloaded_at is None:
            noc.first_downloaded_at = now

        self.audit.record(
            AuditAction.NOC_DOWNLOADED,
            entity_type="ExitNOC",
            entity_id=noc.id,
            workflow_id=workflow.id,
            payload={"noc_number": noc.noc_number, "download_count": noc.download_count},
        )

        if workflow.state is S.NOC_ISSUED:
            await self._workflow_service().complete(
                workflow, note="NOC downloaded by tenant; workflow completed"
            )
        return noc


def _build_snapshot(
    workflow: ExitWorkflow,
    settlement: DepositSettlement,
    noc_number: str,
    issued_at: datetime,
) -> dict:
    return {
        "noc_number": noc_number,
        "issued_at": issued_at.isoformat(),
        "workflow_reference": workflow.reference,
        "contract_number": workflow.contract.contract_number,
        "property": {
            "reference": workflow.property.reference,
            "address": workflow.property.full_address,
        },
        "tenant": {"name": workflow.tenant.full_name, "email": workflow.tenant.email},
        "owner": {"name": workflow.owner.full_name, "email": workflow.owner.email},
        "move_out_date": workflow.move_out_date.isoformat(),
        "settlement": {
            "currency": settlement.currency,
            "deposit_fils": settlement.deposit_fils,
            "total_deductions_fils": settlement.total_deductions_fils,
            "refund_fils": settlement.refund_fils,
            "balance_due_fils": settlement.balance_due_fils,
            "closed_at": settlement.closed_at.isoformat() if settlement.closed_at else None,
        },
    }


def render_noc_pdf(
    workflow: ExitWorkflow,
    settlement: DepositSettlement,
    report: DamageReport | None,
    noc_number: str,
    issued_at: datetime,
) -> bytes:
    """Lay out the certificate. Deterministic for a given set of inputs."""
    blocks: list[pdf.Block] = [
        pdf.Heading("EXIT NO OBJECTION CERTIFICATE", size=17, centered=True, space_before=0),
        pdf.Paragraph(noc_number, size=11, bold=True, centered=True, space_after=2),
        pdf.Paragraph(
            f"Issued {issued_at.strftime('%d %B %Y at %H:%M UTC')}",
            size=8.5,
            centered=True,
            space_after=0,
        ),
        pdf.Rule(space_before=10),
        pdf.Paragraph(
            "This certificate confirms that the tenancy identified below has been terminated, "
            "the property has been inspected, and the security deposit has been settled in "
            "full in accordance with the tenancy contract.",
            space_after=8,
        ),
        pdf.Heading("Parties and property", size=11, space_before=6, space_after=4),
        pdf.KeyValue("Workflow reference", workflow.reference),
        pdf.KeyValue("Tenancy contract", workflow.contract.contract_number),
        pdf.KeyValue("Tenant", workflow.tenant.full_name),
        pdf.KeyValue("Owner", workflow.owner.full_name),
        pdf.KeyValue("Property reference", workflow.property.reference),
        pdf.KeyValue("Property address", workflow.property.full_address),
        pdf.KeyValue("Move-out date", workflow.move_out_date.strftime("%d %B %Y")),
        pdf.Rule(),
        pdf.Heading("Deposit settlement", size=11, space_before=0, space_after=4),
    ]

    if report is not None and report.line_items:
        blocks.append(
            pdf.Paragraph("Deductions assessed at inspection:", size=9.5, bold=True, space_after=3)
        )
        for item in sorted(report.line_items, key=lambda i: i.code):
            label = f"  {item.code} - {item.description}"
            if item.location:
                label = f"{label} ({item.location})"
            blocks.append(
                pdf.KeyValue(
                    label[:90],
                    format_aed(item.amount_fils),
                    size=9.0,
                    label_width=330.0,
                    space_after=1,
                )
            )
        blocks.append(pdf.Spacer(6))
    elif report is not None:
        blocks.append(
            pdf.Paragraph(
                "The inspection recorded no chargeable damage.", size=9.5, space_after=6
            )
        )

    blocks += [
        pdf.KeyValue("Security deposit held", format_aed(settlement.deposit_fils)),
        pdf.KeyValue("Total deductions", format_aed(settlement.total_deductions_fils)),
        pdf.KeyValue(
            "Refund released to tenant", format_aed(settlement.refund_fils), bold_value=True
        ),
    ]
    if settlement.balance_due_fils > 0:
        blocks.append(
            pdf.KeyValue(
                "Balance settled by tenant",
                format_aed(settlement.balance_due_fils),
                bold_value=True,
            )
        )
    if settlement.closed_at is not None:
        blocks.append(
            pdf.KeyValue("Settlement closed", settlement.closed_at.strftime("%d %B %Y %H:%M UTC"))
        )

    blocks += [
        pdf.Rule(),
        pdf.Heading("Declaration", size=11, space_before=0, space_after=4),
        pdf.Paragraph(
            "The owner confirms no further claim against the tenant in respect of this "
            "tenancy, and the tenant confirms no further claim in respect of the security "
            "deposit. Both parties are released from their obligations under the tenancy "
            "contract with effect from the move-out date stated above.",
            space_after=10,
        ),
        pdf.Paragraph(
            "This is a system-generated document and is valid without a signature. Its "
            f"authenticity can be verified against certificate number {noc_number} in the "
            "Meridian platform.",
            size=8.5,
            space_after=0,
        ),
    ]
    return pdf.render_pdf(
        blocks,
        title=f"Exit NOC {noc_number}",
        subject=f"Exit No Objection Certificate for {workflow.reference}",
        created_at=issued_at,
    )
