"""Monetary values.

Money is stored and computed exclusively in *fils* (integer minor units, 1 AED = 100 fils).
Floating point is never used for money anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Self

FILS_PER_AED = 100
DEFAULT_CURRENCY = "AED"


class MoneyError(ValueError):
    """Raised when a monetary value cannot be represented exactly."""


def aed_to_fils(amount: Decimal | int | str) -> int:
    """Convert an AED amount to integer fils, rejecting sub-fils precision."""
    try:
        dec = Decimal(str(amount))
    except InvalidOperation as exc:  # pragma: no cover - defensive
        raise MoneyError(f"not a valid AED amount: {amount!r}") from exc
    if not dec.is_finite():
        raise MoneyError(f"not a finite AED amount: {amount!r}")
    scaled = dec * FILS_PER_AED
    rounded = scaled.to_integral_value(rounding=ROUND_HALF_UP)
    if scaled != rounded:
        raise MoneyError(f"amount {dec} has sub-fils precision and cannot be stored exactly")
    return int(rounded)


def fils_to_aed(fils: int) -> Decimal:
    """Convert integer fils to an exact AED ``Decimal`` with 2 decimal places."""
    return (Decimal(fils) / FILS_PER_AED).quantize(Decimal("0.01"))


def format_aed(fils: int) -> str:
    """Human-readable amount used on the NOC and in notifications."""
    return f"{fils_to_aed(fils):,.2f} {DEFAULT_CURRENCY}"


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact, non-negative-capable monetary amount in a single currency."""

    fils: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        if not isinstance(self.fils, int) or isinstance(self.fils, bool):
            raise MoneyError("Money.fils must be an int")

    @classmethod
    def from_aed(cls, amount: Decimal | int | str, currency: str = DEFAULT_CURRENCY) -> Self:
        return cls(aed_to_fils(amount), currency)

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> Self:
        return cls(0, currency)

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise MoneyError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.fils + other.fils, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.fils - other.fils, self.currency)

    def clamped_to_zero(self) -> Money:
        return self if self.fils >= 0 else Money(0, self.currency)

    @property
    def is_zero(self) -> bool:
        return self.fils == 0

    @property
    def is_negative(self) -> bool:
        return self.fils < 0

    def to_aed(self) -> Decimal:
        return fils_to_aed(self.fils)

    def __str__(self) -> str:
        return f"{fils_to_aed(self.fils):,.2f} {self.currency}"
