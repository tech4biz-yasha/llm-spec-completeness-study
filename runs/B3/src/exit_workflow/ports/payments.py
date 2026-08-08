"""Payment gateway port. rules.yaml#EXIT-08, algorithm.md#10-11.

The gateway owns the money movement; this module owns only the local ``payments`` row and
the rule that the NOC waits for SUCCEEDED. The idempotency key is the workflow ID
(rules.yaml#EXIT-08, edges.yaml#X-005) and is passed on every call, so a retried request
can never produce a second disbursement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..enums import PaymentStatus


class GatewayError(RuntimeError):
    """The gateway could not be reached or returned an unusable response."""


@dataclass(frozen=True, slots=True)
class GatewayResult:
    status: PaymentStatus
    reference: str | None = None
    failure_reason: str | None = None


@runtime_checkable
class PaymentGateway(Protocol):
    def initiate_refund(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        beneficiary_id: str,
        metadata: dict[str, str],
    ) -> GatewayResult:
        """Create (or return the existing) DEPOSIT_REFUND disbursement for the key."""

    def get_status(self, *, idempotency_key: str) -> GatewayResult:
        """Current status of the disbursement identified by the idempotency key."""
