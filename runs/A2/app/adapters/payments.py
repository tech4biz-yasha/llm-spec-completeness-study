"""Payment gateway adapters for deposit refunds."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

import httpx

from app.core.logging import get_logger
from app.ports.payments import (
    PaymentGateway,
    PaymentGatewayError,
    PayoutRequest,
    PayoutResult,
    PayoutState,
)

log = get_logger(__name__)


class NullPaymentGateway(PaymentGateway):
    """In-process gateway used locally and in tests.

    Accepts every payout and reports PENDING, matching the real asynchronous contract:
    the terminal state must still arrive through the webhook, so no test accidentally
    depends on a synchronous success that production would never produce.
    """

    provider = "null"

    def __init__(self) -> None:
        self.requests: list[PayoutRequest] = []
        self._by_key: dict[str, PayoutResult] = {}

    async def initiate_payout(self, request: PayoutRequest) -> PayoutResult:
        if (existing := self._by_key.get(request.idempotency_key)) is not None:
            return existing
        if request.amount < Decimal("0"):
            raise PaymentGatewayError("payout amount must not be negative", retryable=False)
        self.requests.append(request)
        reference = "null_" + hashlib.sha256(
            request.idempotency_key.encode()
        ).hexdigest()[:24]
        result = PayoutResult(
            state=PayoutState.PENDING,
            provider=self.provider,
            provider_reference=reference,
        )
        self._by_key[request.idempotency_key] = result
        return result

    async def get_payout(self, provider_reference: str) -> PayoutResult:
        for result in self._by_key.values():
            if result.provider_reference == provider_reference:
                return result
        raise PaymentGatewayError(f"unknown payout {provider_reference}", retryable=False)


class HttpPaymentGateway(PaymentGateway):
    """Talks to the platform's Payment Service over HTTP.

    The SRS assigns settlement to the Payment service without pinning a provider API, so
    this adapter speaks a small, explicit contract; swap it for a provider SDK by
    implementing :class:`app.ports.payments.PaymentGateway`.
    """

    provider = "payment-service"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client = client

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            response = await client.request(method, f"{self._base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise PaymentGatewayError(f"payment service unreachable: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code >= 500:
            raise PaymentGatewayError(
                f"payment service returned {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise PaymentGatewayError(
                f"payment service rejected the request: {response.status_code} "
                f"{response.text[:200]}",
                retryable=False,
            )
        try:
            body: dict[str, Any] = response.json()
        except ValueError as exc:
            raise PaymentGatewayError("payment service returned invalid JSON") from exc
        return body

    async def initiate_payout(self, request: PayoutRequest) -> PayoutResult:
        body = await self._request(
            "POST",
            "/v1/payouts",
            headers=self._headers(request.idempotency_key),
            json={
                "amount": str(request.amount),
                "currency": request.currency,
                "beneficiary_ref": request.beneficiary_ref,
                "reference": request.reference,
                "description": request.description,
                "metadata": request.metadata,
            },
        )
        return self._to_result(body)

    async def get_payout(self, provider_reference: str) -> PayoutResult:
        body = await self._request(
            "GET", f"/v1/payouts/{provider_reference}", headers=self._headers()
        )
        return self._to_result(body)

    def _to_result(self, body: dict[str, Any]) -> PayoutResult:
        raw_status = str(body.get("status", "")).upper()
        mapping = {
            "PENDING": PayoutState.PENDING,
            "PROCESSING": PayoutState.PENDING,
            "SUBMITTED": PayoutState.PENDING,
            "SUCCEEDED": PayoutState.SUCCEEDED,
            "PAID": PayoutState.SUCCEEDED,
            "COMPLETED": PayoutState.SUCCEEDED,
            "FAILED": PayoutState.FAILED,
            "REJECTED": PayoutState.FAILED,
            "CANCELLED": PayoutState.FAILED,
        }
        state = mapping.get(raw_status)
        if state is None:
            raise PaymentGatewayError(
                f"unrecognised payout status {raw_status!r}", retryable=False
            )
        reference = body.get("id") or body.get("reference")
        if not reference:
            raise PaymentGatewayError("payment service response has no payout id")
        return PayoutResult(
            state=state,
            provider=self.provider,
            provider_reference=str(reference),
            failure_code=body.get("failure_code"),
            failure_reason=body.get("failure_reason"),
        )
