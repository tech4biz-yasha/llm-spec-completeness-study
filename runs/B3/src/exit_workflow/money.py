"""AED money handling.

AGENTS.md, Conventions: "Money is Decimal, minor units in storage, AED, 2 decimal
places. Never float."

Storage is an integer count of fils (1 AED = 100 fils). Every value that crosses the
API boundary is a ``Decimal`` quantized to 2 dp with ``ROUND_HALF_UP``
(rules.yaml#EXIT-07).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

CURRENCY: Final[str] = "AED"
MINOR_UNITS_PER_MAJOR: Final[int] = 100
_EXPONENT: Final[Decimal] = Decimal("0.01")


def quantize(amount: Decimal) -> Decimal:
    """Round to 2 dp, half-up. rules.yaml#EXIT-07."""
    return amount.quantize(_EXPONENT, rounding=ROUND_HALF_UP)


def to_minor(amount: Decimal) -> int:
    """Convert an AED Decimal to whole fils, rounding half-up at 2 dp."""
    if not isinstance(amount, Decimal):  # pragma: no cover - type guard, never float
        raise TypeError(f"money must be Decimal, got {type(amount).__name__}")
    try:
        return int(quantize(amount) * MINOR_UNITS_PER_MAJOR)
    except (InvalidOperation, ValueError) as exc:  # pragma: no cover
        raise ValueError(f"not a representable AED amount: {amount!r}") from exc


def to_major(minor: int) -> Decimal:
    """Convert whole fils back to an AED Decimal with 2 dp."""
    return quantize(Decimal(minor) / MINOR_UNITS_PER_MAJOR)


def refund_minor(security_deposit_minor: int, confirmed_damage_minor: int) -> int:
    """refund = max(security_deposit - confirmed_damage, 0). rules.yaml#EXIT-07.

    The caller is responsible for raising SpecUnresolved("R8") before reaching here when
    confirmed_damage > security_deposit; the ``max`` is retained because EXIT-07 states
    the formula that way.
    """
    return max(security_deposit_minor - confirmed_damage_minor, 0)
