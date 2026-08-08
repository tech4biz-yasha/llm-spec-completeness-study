"""Money handling.

AGENTS.md: "Money is Decimal, minor units in storage, AED, 2 decimal places.
Never float."

Storage is an ``int`` count of fils (AED minor units). The domain and the API
speak ``Decimal`` with exactly 2 decimal places, rounded ROUND_HALF_UP
(rules.yaml#EXIT-07).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final

CURRENCY: Final[str] = "AED"
MINOR_UNITS_PER_MAJOR: Final[int] = 100
EXPONENT: Final[Decimal] = Decimal("0.01")


class MoneyError(ValueError):
    """Raised when a value cannot be represented as AED money."""


def quantize(amount: Decimal) -> Decimal:
    """Round to 2 dp, half-up. rules.yaml#EXIT-07."""
    if not isinstance(amount, Decimal):  # float is never acceptable — AGENTS.md
        raise MoneyError(f"money must be Decimal, got {type(amount).__name__}")
    try:
        return amount.quantize(EXPONENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:  # NaN / Infinity / overflow
        raise MoneyError(f"not a representable amount: {amount!r}") from exc


def to_minor(amount: Decimal) -> int:
    """Decimal AED -> integer fils, half-up at 2 dp."""
    return int(quantize(amount) * MINOR_UNITS_PER_MAJOR)


def from_minor(minor: int) -> Decimal:
    """Integer fils -> Decimal AED, exactly 2 dp."""
    if not isinstance(minor, int) or isinstance(minor, bool):
        raise MoneyError(f"minor units must be int, got {type(minor).__name__}")
    return (Decimal(minor) / MINOR_UNITS_PER_MAJOR).quantize(EXPONENT)


def format_minor(minor: int) -> str:
    """Human/audit rendering, e.g. 'AED 1234.50'."""
    return f"{CURRENCY} {from_minor(minor)}"
