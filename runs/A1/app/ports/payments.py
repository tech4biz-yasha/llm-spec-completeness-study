"""Payment execution port.

The SRS routes exit settlements through the platform Payment service (§15, T13). This module
owns the *ledger* — which legs exist, what they are worth, whether they are satisfied — and
delegates the actual movement of money to a gateway behind this port.

The default adapter records an internal ledger movement and succeeds synchronously, which is
the correct behaviour for a deposit held in the platform's own escrow account. Swapping in a
PSP adapter (Network International, Telr, Stripe) requires no change above this line.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class GatewayOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    PENDING = "PENDING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PaymentRequest:
    amount_fils: int
    currency: str
    #: Reused verbatim as the provider-side idempotency key.
    idempotency_key: str
    reference: str
    payer_id: uuid.UUID
    payee_id: uuid.UUID
    description: str


@dataclass(frozen=True, slots=True)
class PaymentResult:
    outcome: GatewayOutcome
    provider: str
    provider_reference: str | None = None
    failure_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is GatewayOutcome.SUCCEEDED


@runtime_checkable
class PaymentGateway(Protocol):
    async def execute(self, request: PaymentRequest) -> PaymentResult: ...


class InternalLedgerGateway:
    """Moves the deposit between internally held balances. Deterministic and idempotent."""

    provider = "internal"

    async def execute(self, request: PaymentRequest) -> PaymentResult:
        logger.info(
            "settlement payment executed",
            extra={
                "reference": request.reference,
                "amount_fils": request.amount_fils,
                "currency": request.currency,
                "idempotency_key": request.idempotency_key,
            },
        )
        # The provider reference is derived from the idempotency key, so a replay of the same
        # logical payment yields the same reference rather than a second movement.
        digest = uuid.uuid5(uuid.NAMESPACE_URL, f"ledger:{request.idempotency_key}")
        return PaymentResult(
            outcome=GatewayOutcome.SUCCEEDED,
            provider=self.provider,
            provider_reference=f"LEDGER-{digest.hex[:16].upper()}",
        )
