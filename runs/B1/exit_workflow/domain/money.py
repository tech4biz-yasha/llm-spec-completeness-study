"""Money handling.

AGENTS.md: "Money is Decimal, minor units in storage, AED, 2 decimal places.
Never float."

Storage is an integer count of fils (1 AED = 100 fils) in a ``BIGINT`` column;
the boundary converts to :class:`~decimal.Decimal` with two decimal places.
There is no float anywhere in this module, and :func:`to_minor` rejects float
input outright rather than silently accepting a value that has already lost
precision.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final

CURRENCY: Final[str] = "AED"
MINOR_UNITS_PER_MAJOR: Final[int] = 100
EXPONENT: Final[Decimal] = Decimal("0.01")

#: Guard against overflowing a BIGINT column; ~92 quadrillion fils.
MAX_MINOR: Final[int] = 2**63 - 1


class MoneyError(ValueError):
    """An amount is not a usable AED value."""


def quantize(amount: Decimal) -> Decimal:
    """Round to 2 dp, half-up (rules.yaml#EXIT-07)."""
    try:
        return amount.quantize(EXPONENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise MoneyError(f"amount {amount} cannot be expressed to 2 decimal places") from exc


def to_minor(amount: Decimal | int | str) -> int:
    """Convert an AED amount to integer fils, rounding half-up at 2 dp."""
    if isinstance(amount, float):  # noqa: SIM101 - explicit: floats are never money
        raise MoneyError("float is not an acceptable money representation; use Decimal")
    if isinstance(amount, bool):
        raise MoneyError("bool is not an acceptable money representation")
    if not isinstance(amount, Decimal):
        try:
            amount = Decimal(amount)
        except InvalidOperation as exc:
            raise MoneyError(f"{amount!r} is not a valid AED amount") from exc
    if not amount.is_finite():
        raise MoneyError(f"{amount} is not a finite amount")
    if amount < 0:
        raise MoneyError(f"negative amount {amount} is not permitted")

    minor = int(quantize(amount) * MINOR_UNITS_PER_MAJOR)
    if minor > MAX_MINOR:
        raise MoneyError(f"amount {amount} exceeds the storable range")
    return minor


def from_minor(minor: int) -> Decimal:
    """Convert integer fils back to a 2 dp AED :class:`Decimal`."""
    if not isinstance(minor, int) or isinstance(minor, bool):
        raise MoneyError(f"minor units must be int, got {type(minor).__name__}")
    return quantize(Decimal(minor) / MINOR_UNITS_PER_MAJOR)


def format_aed(minor: int) -> str:
    """Render an amount for documents and operator messages, e.g. ``AED 12,500.00``."""
    return f"{CURRENCY} {from_minor(minor):,.2f}"
