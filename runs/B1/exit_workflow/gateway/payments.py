"""Payment gateway port.

rules.yaml#EXIT-08 requires the refund to be a payment transaction whose
idempotency key is the workflow ID, and requires the NOC to wait until "the
gateway confirms SUCCEEDED". It does not name a provider — risks.md records the
payment-gateway selection as still open — so this module defines the port and
leaves the adapter to the payments platform.

The default binding is :class:`UnconfiguredPaymentGateway`, which raises. A
module that quietly reported PENDING with no gateway attached would look like a
slow refund rather than a missing integration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from exit_workflow.domain.enums import PaymentStatus


class GatewayError(RuntimeError):
    """The gateway could not be reached or returned an unusable response."""


@dataclass(frozen=True, slots=True)
class GatewayResult:
    """Outcome of a gateway call."""

    status: PaymentStatus
    reference: str | None = None
    failure_reason: str | None = None


class PaymentGateway(Protocol):
    """Refund submission and status enquiry."""

    async def submit_refund(
        self,
        *,
        payment_id: uuid.UUID,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        payee_id: uuid.UUID,
    ) -> GatewayResult:
        """Submit a refund. Must be idempotent on ``idempotency_key``."""

    async def fetch_status(self, *, idempotency_key: str, reference: str | None) -> GatewayResult:
        """Return the current status of a previously submitted refund."""


class UnconfiguredPaymentGateway:
    """Fail-closed default."""

    async def submit_refund(
        self,
        *,
        payment_id: uuid.UUID,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        payee_id: uuid.UUID,
    ) -> GatewayResult:
        raise GatewayError(
            "no payment gateway is configured; deposit refunds cannot be submitted "
            "(rules.yaml#EXIT-08)"
        )

    async def fetch_status(self, *, idempotency_key: str, reference: str | None) -> GatewayResult:
        raise GatewayError("no payment gateway is configured; refund status cannot be read")
