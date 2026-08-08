"""Shared response primitives."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from app.money import DEFAULT_CURRENCY, fils_to_aed


class MoneyOut(BaseModel):
    """A monetary amount.

    ``fils`` is authoritative — integer minor units, safe for arithmetic. ``amount`` is the
    same value as exact AED for display; clients must not do arithmetic on it.
    """

    model_config = ConfigDict(frozen=True)

    fils: int = Field(description="Integer minor units (1 AED = 100 fils)")
    amount: Decimal = Field(description="Exact decimal amount, for display only")
    currency: str = DEFAULT_CURRENCY

    @classmethod
    def of(cls, fils: int, currency: str = DEFAULT_CURRENCY) -> Self:
        return cls(fils=fils, amount=fils_to_aed(fils), currency=currency)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class PageMeta(BaseModel):
    limit: int
    offset: int
    count: int
