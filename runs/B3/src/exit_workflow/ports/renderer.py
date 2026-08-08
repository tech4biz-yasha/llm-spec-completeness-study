"""NOC document renderer port. rules.yaml#EXIT-09.

The kit says the NOC is "a PDF, stored in the UAE region bucket, immutable once issued,
linked on the workflow". It does not give a template, wording, letterhead, signatory or
language. Those are not invented here: ``NocContext`` carries only facts already held on
the workflow, and the default renderer lays them out plainly. A deployment that has the
approved template supplies its own ``NocRenderer``. See blockers.md#B-8.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class NocContext:
    workflow_id: str
    contract_id: str
    property_id: str
    tenant_id: str
    owner_id: str
    move_out_date: date
    refund_amount: Decimal
    currency: str
    payment_id: str
    payment_reference: str | None
    issued_at: datetime


@runtime_checkable
class NocRenderer(Protocol):
    def render(self, context: NocContext) -> bytes:
        """Return the NOC as PDF bytes."""
