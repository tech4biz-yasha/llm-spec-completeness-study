"""Payment gateway port for the deposit payout (O16).

The transaction id doubles as the gateway idempotency key, so a retry after a
timeout re-attaches to the original transfer instead of creating a second one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from exit_workflow.core.errors import UpstreamServiceError

SERVICE_NAME = "payment"


@dataclass(frozen=True, slots=True)
class PayoutRequest:
    transaction_id: uuid.UUID
    amount: Decimal
    currency: str
    destination_token: str | None
    reference: str
    description: str
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PayoutResult:
    succeeded: bool
    gateway_reference: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(Protocol):
    name: str

    async def payout(self, request: PayoutRequest) -> PayoutResult: ...


class SimulatedPaymentGateway:
    """Local/test adapter.

    Succeeds deterministically. A destination token prefixed ``FAIL-`` forces a
    declined payout so the failure path can be exercised end to end.
    """

    name = "simulated"

    async def payout(self, request: PayoutRequest) -> PayoutResult:
        token = request.destination_token or ""
        if token.upper().startswith("FAIL-"):
            return PayoutResult(
                succeeded=False,
                failure_code="DESTINATION_REJECTED",
                failure_message="Payout destination was rejected by the bank.",
                raw={"simulated": True},
            )
        return PayoutResult(
            succeeded=True,
            gateway_reference=f"SIMPAY-{request.transaction_id.hex[:16].upper()}",
            raw={"simulated": True, "amount": str(request.amount), "currency": request.currency},
        )


class HttpPaymentGateway:
    """Adapter for the platform Payment service."""

    name = "meridian-payments"

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    async def payout(self, request: PayoutRequest) -> PayoutResult:
        import httpx

        headers = {"Idempotency-Key": str(request.transaction_id)}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        body = {
            "amount": str(request.amount),
            "currency": request.currency,
            "destination_token": request.destination_token,
            "reference": request.reference,
            "description": request.description,
            "metadata": request.metadata,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/internal/payouts", json=body, headers=headers
                )
        except httpx.HTTPError as exc:
            # Network-level failure: the transfer may or may not have happened.
            # Surfaced as retryable; the shared idempotency key makes the retry
            # safe.
            raise UpstreamServiceError(SERVICE_NAME, str(exc)) from exc

        if response.status_code >= 500:
            raise UpstreamServiceError(
                SERVICE_NAME, f"Payment service returned HTTP {response.status_code}."
            )
        payload = response.json()
        if response.status_code >= 400 or payload.get("status") == "FAILED":
            return PayoutResult(
                succeeded=False,
                failure_code=payload.get("code", "PAYOUT_DECLINED"),
                failure_message=payload.get("message", "Payout was declined."),
                raw=_redact(payload),
            )
        return PayoutResult(
            succeeded=True,
            gateway_reference=payload.get("payout_id") or payload.get("reference"),
            raw=_redact(payload),
        )


_SENSITIVE_KEYS = {"iban", "account_number", "card", "beneficiary_account", "swift"}


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ("***" if k.lower() in _SENSITIVE_KEYS else v)
        for k, v in payload.items()
        if not isinstance(v, (dict, list)) or k.lower() not in _SENSITIVE_KEYS
    }
