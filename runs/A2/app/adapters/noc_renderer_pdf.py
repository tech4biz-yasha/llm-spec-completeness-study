"""Renders the digital Exit NOC as a PDF (SRS T13 step 10, O16)."""

from __future__ import annotations

import asyncio

from app.adapters.pdf import PdfDocument
from app.core.money import format_aed
from app.ports.noc_renderer import NocFacts, NocRenderer

_DISCLAIMER = (
    "This certificate is issued electronically and is valid without a physical "
    "signature. Its authenticity can be confirmed using the verification code below."
)

_BODY = (
    "This is to certify that the tenancy identified below has been terminated by mutual "
    "completion of the exit process. The property has been inspected, the security "
    "deposit has been settled in accordance with the inspection findings, and the "
    "landlord confirms that no objection remains in respect of the tenant's vacating of "
    "the premises."
)


class PdfNocRenderer(NocRenderer):
    """Deterministic single-file PDF renderer.

    Rendering is CPU-bound and short (~1ms), but it is dispatched to a worker thread so a
    burst of NOC issuances cannot stall the event loop and blow the p95 budget.
    """

    async def render(self, facts: NocFacts) -> bytes:
        return await asyncio.to_thread(self._render_sync, facts)

    def _render_sync(self, facts: NocFacts) -> bytes:
        doc = PdfDocument(
            title=f"Exit NOC {facts.noc_number}",
            author=facts.issuer_name,
            created_at=facts.issued_at,
        )

        # ---------------------------------------------------------- header
        doc.line(facts.issuer_name, font="bold", size=15)
        doc.line(facts.issuer_address, size=9, gray=0.45)
        doc.rule()

        doc.space(6)
        doc.line("EXIT NO OBJECTION CERTIFICATE", font="bold", size=15, align="center")
        doc.line(
            f"Certificate No. {facts.noc_number}", size=10, align="center", gray=0.35
        )
        doc.space(6)
        doc.rule()

        # ------------------------------------------------------ issuance
        doc.space(4)
        doc.key_value("Issued on", facts.issued_at.strftime("%d %B %Y, %H:%M UTC"))
        doc.key_value("Effective from", facts.effective_date.strftime("%d %B %Y"))
        doc.key_value("Exit workflow ref.", facts.workflow_reference)
        doc.key_value("Tenancy contract ref.", facts.contract_reference)

        # -------------------------------------------------------- parties
        doc.space(10)
        doc.line("PARTIES AND PROPERTY", font="bold", size=11)
        doc.space(2)
        doc.key_value("Tenant", facts.tenant_name)
        if facts.tenant_identifier:
            doc.key_value("Tenant ID", facts.tenant_identifier)
        doc.key_value("Landlord / Owner", facts.owner_name)
        doc.key_value("Property", facts.property_reference)
        doc.key_value("Address", facts.property_address)
        doc.key_value("Move-out date", facts.move_out_date.strftime("%d %B %Y"))

        # ----------------------------------------------------- inspection
        if facts.inspection_date or facts.inspection_agency:
            doc.space(10)
            doc.line("INSPECTION", font="bold", size=11)
            doc.space(2)
            if facts.inspection_agency:
                doc.key_value("Inspecting agency", facts.inspection_agency)
            if facts.inspection_date:
                doc.key_value("Inspection date", facts.inspection_date.strftime("%d %B %Y"))

        # ----------------------------------------------- deposit settlement
        doc.space(10)
        doc.line("SECURITY DEPOSIT SETTLEMENT", font="bold", size=11)
        doc.space(4)
        cur = facts.currency
        doc.money_row("Security deposit held", format_aed(facts.deposit_amount, cur))

        if facts.deduction_lines:
            doc.space(2)
            doc.line("Deductions", font="bold", size=9.5, gray=0.35)
            for item in facts.deduction_lines:
                label = f"{item.description} ({item.category.replace('_', ' ').title()})"
                doc.money_row(
                    label, format_aed(item.amount, cur), size=9.5, indent=14, leading=14
                )
        doc.space(2)
        doc.money_row("Total deductions", format_aed(facts.total_deductions, cur))
        doc.rule(gap=5)
        doc.money_row(
            "Net amount refunded to tenant", format_aed(facts.net_refund_amount, cur), bold=True
        )
        if facts.tenant_liability_amount > 0:
            doc.money_row(
                "Balance recoverable from tenant",
                format_aed(facts.tenant_liability_amount, cur),
                bold=True,
            )

        if facts.settlement_paid_at:
            doc.space(4)
            doc.key_value(
                "Refund settled on", facts.settlement_paid_at.strftime("%d %B %Y, %H:%M UTC")
            )
        if facts.payment_reference:
            doc.key_value("Payment reference", facts.payment_reference)

        # ------------------------------------------------------ narrative
        doc.space(12)
        doc.line("DECLARATION", font="bold", size=11)
        doc.space(2)
        doc.paragraph(_BODY, size=10, leading=14.5)

        for note in facts.notes:
            doc.space(4)
            doc.paragraph(note, size=9.5, leading=13.5, gray=0.3)

        # ------------------------------------------------------ footer
        doc.space(16)
        doc.rule()
        doc.space(2)
        doc.paragraph(_DISCLAIMER, size=8.5, leading=12, gray=0.4)
        doc.space(4)
        doc.key_value("Verification code", facts.verification_code, size=9.5, label_width=110)
        if facts.verification_url:
            doc.key_value("Verify at", facts.verification_url, size=9.5, label_width=110)

        return doc.build()
