"""Payment gateway adapters. rules.yaml#EXIT-08.

No real gateway is named by the kit — O18 records the Central Bank / Al Etihad decision as
still pending (risks.md, "Open items also flagged in the SRS itself"), so no provider
integration is written here. ``ScriptedPaymentGateway`` is a deterministic test double
that honours the idempotency key exactly as a real gateway must.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..enums import PaymentStatus
from ..ports.payments import GatewayResult


@dataclass
class ScriptedPaymentGateway:
    """Returns ``default_status`` unless an override is registered for a key.

    Honouring the idempotency key is the point: ``initiate_refund`` called twice with the
    same key returns the same disbursement and never creates a second one
    (rules.yaml#EXIT-08, edges.yaml#X-005).
    """

    default_status: PaymentStatus = PaymentStatus.SUCCEEDED
    overrides: dict[str, GatewayResult] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    _disbursements: dict[str, GatewayResult] = field(default_factory=dict, init=False)

    def set_status(self, idempotency_key: str, result: GatewayResult) -> None:
        self.overrides[idempotency_key] = result
        if idempotency_key in self._disbursements:
            self._disbursements[idempotency_key] = result

    def initiate_refund(
        self,
        *,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        beneficiary_id: str,
        metadata: dict[str, str],
    ) -> GatewayResult:
        self.calls.append(idempotency_key)
        if idempotency_key in self._disbursements:
            return self._disbursements[idempotency_key]
        result = self.overrides.get(
            idempotency_key,
            GatewayResult(status=self.default_status, reference=f"gw-{idempotency_key}"),
        )
        if result.reference is None and result.status is not PaymentStatus.FAILED:
            result = GatewayResult(
                status=result.status,
                reference=f"gw-{idempotency_key}",
                failure_reason=result.failure_reason,
            )
        self._disbursements[idempotency_key] = result
        return result

    def get_status(self, *, idempotency_key: str) -> GatewayResult:
        if idempotency_key in self.overrides:
            return self.overrides[idempotency_key]
        return self._disbursements.get(idempotency_key, GatewayResult(status=PaymentStatus.PENDING))
