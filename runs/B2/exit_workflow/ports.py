"""Outbound ports.

Everything this module needs from the outside world is declared here as a
Protocol and injected. Two of these have no concrete implementation in this
repository *on purpose*:

* ``PaymentGateway`` — the payment gateway is not chosen yet (risks.md, Appendix A:
  "Finalised payment modes — O18 records a pending decision on Central Bank and
  Al Etihad gateways"). Writing an adapter would mean inventing its contract.
* ``ExitReasonReference`` — the exit reason reference list is an open item
  (risks.md, Appendix A: "Reference data dictionary, specifically exit reasons").
  The module validates against whatever list is supplied and hard-codes none.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Events — rules.yaml#EXIT-04, Kafka producer
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutboundEvent:
    topic: str
    key: str
    event_type: str
    payload: dict[str, Any]


@runtime_checkable
class EventPublisher(Protocol):
    """Kafka producer seam. Raising signals a dispatch failure and triggers retry."""

    async def publish(self, event: OutboundEvent) -> None: ...


# ---------------------------------------------------------------------------
# Payments — rules.yaml#EXIT-08
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefundRequest:
    """A DEPOSIT_REFUND instruction."""

    idempotency_key: str  # rules.yaml#EXIT-08 — always the workflow id
    workflow_id: str
    contract_id: str
    tenant_id: str
    amount: Decimal  # AED, 2 dp
    currency: str


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """Gateway outcome. algorithm.md step 11 — only SUCCEEDED lets the flow continue."""

    status: str  # 'PENDING' | 'SUCCEEDED' | 'FAILED'
    reference: str | None = None
    failure_reason: str | None = None


@runtime_checkable
class PaymentGateway(Protocol):
    """Deliberately abstract — see module docstring (risks.md Appendix A)."""

    async def initiate_refund(self, request: RefundRequest) -> GatewayResult: ...

    async def get_status(self, idempotency_key: str) -> GatewayResult: ...


# ---------------------------------------------------------------------------
# NOC — rules.yaml#EXIT-09
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NocContext:
    """The facts printed on the NOC. Only fields the spec establishes."""

    workflow_id: str
    contract_id: str
    property_id: str
    tenant_id: str
    owner_id: str
    move_out_date: date
    security_deposit: Decimal
    confirmed_damage: Decimal
    refund_amount: Decimal
    currency: str
    payment_reference: str
    issued_at_dubai: str


@runtime_checkable
class NocRenderer(Protocol):
    """Renders the NOC as a PDF (rules.yaml#EXIT-09)."""

    def render(self, context: NocContext) -> bytes: ...


@dataclass(frozen=True, slots=True)
class StoredObject:
    bucket: str
    key: str
    region: str
    sha256: str
    size_bytes: int


@runtime_checkable
class ObjectStore(Protocol):
    """Write-once object storage in the UAE region bucket (rules.yaml#EXIT-09)."""

    async def put_immutable(self, key: str, body: bytes, content_type: str) -> StoredObject: ...


# ---------------------------------------------------------------------------
# Reference data — rules.yaml#EXIT-02
# ---------------------------------------------------------------------------


@runtime_checkable
class ExitReasonReference(Protocol):
    """Supplies the exit reason reference list.

    Returning ``None`` means the list is not yet defined; initiation then raises
    SpecUnresolved rather than guessing (blockers.md#B-001).
    """

    async def exit_reasons(self) -> Collection[str] | None: ...
