"""Money handling.

All monetary values in this module are AED (SRS: "Currency: AED. UAE market") and are
persisted as ``NUMERIC(14, 2)``. We never let a float touch a monetary value: inbound
JSON numbers are parsed by pydantic into ``Decimal`` and quantised here to 2 places
(fils) using banker-safe ``ROUND_HALF_UP``, which is what UAE invoicing expects.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from app.core.errors import ValidationFailedError

MONEY_EXPONENT = Decimal("0.01")
#: Guard against absurd inputs long before NUMERIC(14,2) would overflow.
MAX_AMOUNT = Decimal("99999999.99")
ZERO = Decimal("0.00")


def quantize(amount: Decimal | int | str) -> Decimal:
    """Round a monetary amount to 2 decimal places."""
    try:
        value = Decimal(amount) if not isinstance(amount, Decimal) else amount
    except (InvalidOperation, TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValidationFailedError(f"{amount!r} is not a valid monetary amount.") from exc
    if not value.is_finite():
        raise ValidationFailedError("Monetary amount must be finite.")
    return value.quantize(MONEY_EXPONENT, rounding=ROUND_HALF_UP)


def validate_non_negative(amount: Decimal, field: str) -> Decimal:
    value = quantize(amount)
    if value < ZERO:
        raise ValidationFailedError(
            f"{field} must not be negative.", details={"field": field, "value": str(value)}
        )
    if value > MAX_AMOUNT:
        raise ValidationFailedError(
            f"{field} exceeds the maximum supported amount.",
            details={"field": field, "max": str(MAX_AMOUNT)},
        )
    return value


def total(amounts: Iterable[Decimal]) -> Decimal:
    """Sum monetary amounts exactly."""
    return quantize(sum(amounts, ZERO))


def settle(deposit: Decimal, deductions: Decimal) -> tuple[Decimal, Decimal]:
    """Split a deposit against deductions.

    Returns ``(net_refund, tenant_liability)``.

    The SRS only describes the happy path ("deposit minus damage"). When assessed
    deductions exceed the held deposit the refund floors at zero and the excess is
    recorded as a tenant liability so it can be pursued outside this module -- it is
    deliberately *not* netted into a negative refund.
    """
    deposit_q = validate_non_negative(deposit, "security_deposit_amount")
    deductions_q = validate_non_negative(deductions, "total_deductions")
    if deductions_q <= deposit_q:
        return quantize(deposit_q - deductions_q), ZERO
    return ZERO, quantize(deductions_q - deposit_q)


def format_aed(amount: Decimal, currency: str = "AED") -> str:
    """Render an amount for human-facing documents (NOC, notifications)."""
    value = quantize(amount)
    whole, _, frac = f"{value:.2f}".partition(".")
    negative = whole.startswith("-")
    digits = whole.lstrip("-")
    grouped = ""
    while len(digits) > 3:
        grouped = "," + digits[-3:] + grouped
        digits = digits[:-3]
    grouped = digits + grouped
    return f"{'-' if negative else ''}{currency} {grouped}.{frac}"
