"""AGENTS.md money convention and rules.yaml#EXIT-07 arithmetic."""

from __future__ import annotations

from decimal import Decimal

import pytest

from exit_workflow.money import CURRENCY, quantize, refund_minor, to_major, to_minor
from exit_workflow.schemas import InspectionReportRequest


def test_currency_is_aed():
    assert CURRENCY == "AED"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.005"), Decimal("0.01")),  # half-up, not banker's
        (Decimal("0.015"), Decimal("0.02")),
        (Decimal("0.025"), Decimal("0.03")),
        (Decimal("2.345"), Decimal("2.35")),
    ],
)
def test_rounding_is_half_up(value, expected):
    """rules.yaml#EXIT-07 — round half-up to 2 dp. Python's default would round half-even."""
    assert quantize(value) == expected


def test_minor_units_round_trip():
    assert to_minor(Decimal("10000.00")) == 1_000_000
    assert to_major(1_000_000) == Decimal("10000.00")
    assert to_major(849_950) == Decimal("8499.50")


def test_floats_are_rejected_by_the_converter():
    """AGENTS.md — "Never float"."""
    with pytest.raises(TypeError):
        to_minor(1500.50)  # type: ignore[arg-type]


def test_refund_formula():
    """rules.yaml#EXIT-07 — refund = max(deposit - damage, 0)."""
    assert refund_minor(1_000_000, 150_050) == 849_950
    assert refund_minor(1_000_000, 1_000_000) == 0
    assert refund_minor(1_000_000, 2_000_000) == 0  # guarded by the R8 branch upstream


def test_request_schema_parses_money_without_binary_float_error():
    parsed = InspectionReportRequest(damage_amount=1500.75, photos=[])
    assert parsed.damage_amount == Decimal("1500.75")
    assert isinstance(parsed.damage_amount, Decimal)


def test_request_schema_accepts_decimal_strings():
    assert InspectionReportRequest(damage_amount="0.01", photos=[]).damage_amount == Decimal("0.01")


@pytest.mark.parametrize("value", ["-1.00", "1.001", "not-a-number", "NaN", "Infinity", True])
def test_request_schema_rejects_bad_money(value):
    with pytest.raises(ValueError):
        InspectionReportRequest(damage_amount=value, photos=[])
