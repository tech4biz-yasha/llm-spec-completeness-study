"""NOC document rendering port."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NocDeductionLine:
    description: str
    category: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class NocFacts:
    """Everything printed on the certificate. Frozen at issuance."""

    noc_number: str
    workflow_reference: str
    issued_at: datetime
    effective_date: date
    issuer_name: str
    issuer_address: str

    tenant_name: str
    tenant_identifier: str | None
    owner_name: str
    property_reference: str
    property_address: str
    contract_reference: str

    move_out_date: date
    inspection_date: date | None
    inspection_agency: str | None

    currency: str
    deposit_amount: Decimal
    total_deductions: Decimal
    net_refund_amount: Decimal
    tenant_liability_amount: Decimal
    deduction_lines: tuple[NocDeductionLine, ...] = ()
    settlement_paid_at: datetime | None = None
    payment_reference: str | None = None

    verification_code: str = ""
    verification_url: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        """Serialisable snapshot stored alongside the PDF."""
        return {
            "noc_number": self.noc_number,
            "workflow_reference": self.workflow_reference,
            "issued_at": self.issued_at.isoformat(),
            "effective_date": self.effective_date.isoformat(),
            "issuer_name": self.issuer_name,
            "tenant_name": self.tenant_name,
            "tenant_identifier": self.tenant_identifier,
            "owner_name": self.owner_name,
            "property_reference": self.property_reference,
            "property_address": self.property_address,
            "contract_reference": self.contract_reference,
            "move_out_date": self.move_out_date.isoformat(),
            "inspection_date": self.inspection_date.isoformat() if self.inspection_date else None,
            "inspection_agency": self.inspection_agency,
            "currency": self.currency,
            "deposit_amount": str(self.deposit_amount),
            "total_deductions": str(self.total_deductions),
            "net_refund_amount": str(self.net_refund_amount),
            "tenant_liability_amount": str(self.tenant_liability_amount),
            "deduction_lines": [
                {"description": d.description, "category": d.category, "amount": str(d.amount)}
                for d in self.deduction_lines
            ],
            "settlement_paid_at": (
                self.settlement_paid_at.isoformat() if self.settlement_paid_at else None
            ),
            "payment_reference": self.payment_reference,
            "verification_code": self.verification_code,
            "verification_url": self.verification_url,
        }


class NocRenderer(Protocol):
    async def render(self, facts: NocFacts) -> bytes:
        """Return the certificate as a PDF byte string."""
        ...
