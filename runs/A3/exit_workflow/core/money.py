"""Money helpers.

All monetary values are ``Decimal`` quantised to 2 dp and persisted as
``NUMERIC(14, 2)``. Floats never touch a money value.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from exit_workflow.core.errors import ValidationError

TWO_PLACES = Decimal("0.01")
ZERO = Decimal("0.00")
MAX_AMOUNT = Decimal("99999999999.99")


def quantize(value: Decimal | int | str) -> Decimal:
    """Round half-up to fils precision (AED sub-unit)."""

    try:
        return Decimal(value).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as exc:  # pragma: no cover - defensive
        raise ValidationError(f"{value!r} is not a valid monetary amount") from exc


def ensure_non_negative(value: Decimal, field: str) -> Decimal:
    amount = quantize(value)
    if amount < ZERO:
        raise ValidationError(f"{field} must not be negative")
    if amount > MAX_AMOUNT:
        raise ValidationError(f"{field} exceeds the maximum supported amount")
    return amount


def subtract_floor_zero(minuend: Decimal, subtrahend: Decimal) -> Decimal:
    """``max(a - b, 0)`` at fils precision."""

    result = quantize(minuend) - quantize(subtrahend)
    return quantize(result) if result > ZERO else ZERO


def format_amount(value: Decimal, currency: str) -> str:
    return f"{currency} {quantize(value):,.2f}"
