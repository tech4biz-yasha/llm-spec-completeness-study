"""Test doubles for the module's outbound ports.

Only ports leave the process: the broker, the payment gateway and the object
store. The database is real PostgreSQL, because most of what the kit demands —
one workflow per contract, one payment per workflow, append-only audit, the
exit-lock transaction — is enforced there and a stubbed database would test
nothing.
"""

from __future__ import annotations

import uuid
from typing import Any

from exit_workflow.domain.enums import PaymentStatus
from exit_workflow.events.publisher import PublishError, encode
from exit_workflow.gateway.payments import GatewayError, GatewayResult


class FakePaymentGateway:
    """Gateway whose answer the test chooses."""

    def __init__(
        self,
        status: PaymentStatus = PaymentStatus.SUCCEEDED,
        *,
        failure_reason: str | None = None,
        raise_error: bool = False,
    ) -> None:
        self.status = status
        self.failure_reason = failure_reason
        self.raise_error = raise_error
        self.submissions: list[dict[str, Any]] = []
        self.status_checks: list[str] = []

    async def submit_refund(
        self,
        *,
        payment_id: uuid.UUID,
        idempotency_key: str,
        amount_minor: int,
        currency: str,
        payee_id: uuid.UUID,
    ) -> GatewayResult:
        if self.raise_error:
            raise GatewayError("gateway unavailable")
        self.submissions.append(
            {
                "payment_id": payment_id,
                "idempotency_key": idempotency_key,
                "amount_minor": amount_minor,
                "currency": currency,
                "payee_id": payee_id,
            }
        )
        return GatewayResult(
            status=self.status,
            reference=f"GW-{idempotency_key}",
            failure_reason=self.failure_reason,
        )

    async def fetch_status(self, *, idempotency_key: str, reference: str | None) -> GatewayResult:
        if self.raise_error:
            raise GatewayError("gateway unavailable")
        self.status_checks.append(idempotency_key)
        return GatewayResult(
            status=self.status,
            reference=reference or f"GW-{idempotency_key}",
            failure_reason=self.failure_reason,
        )


class RecordingEventPublisher:
    """Publisher that records, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published: list[dict[str, Any]] = []
        self.attempts = 0

    async def publish(self, *, topic: str, key: str, payload: dict[str, Any]) -> None:
        self.attempts += 1
        if self.fail:
            raise PublishError("broker unreachable")
        encode(payload)
        self.published.append({"topic": topic, "key": key, "payload": payload})
