"""Payment port for deposit refunds (SRS O16 'Pay Deposit')."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol


class PayoutState(StrEnum):
    #: Accepted by the provider; a webhook will report the outcome.
    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PayoutRequest:
    idempotency_key: str
    amount: Decimal
    currency: str
    beneficiary_ref: str
    reference: str
    description: str
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class PayoutResult:
    state: PayoutState
    provider: str
    provider_reference: str
    failure_code: str | None = None
    failure_reason: str | None = None


class PaymentGateway(Protocol):
    """Deposit refunds are asynchronous: ``initiate_payout`` normally returns PENDING and
    the terminal state arrives on the provider webhook. Implementations must treat
    ``idempotency_key`` as authoritative and return the *original* result for a replay.
    """

    async def initiate_payout(self, request: PayoutRequest) -> PayoutResult: ...

    async def get_payout(self, provider_reference: str) -> PayoutResult: ...


class PaymentGatewayError(RuntimeError):
    """The provider could not be reached or returned an unusable response."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable
