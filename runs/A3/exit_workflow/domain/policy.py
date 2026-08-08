"""Business policy: the arithmetic and validation rules of an exit.

Pure functions only — no I/O — so the rules are directly unit-testable and can
be reused by a pricing preview endpoint without touching the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from exit_workflow.core.config import Settings
from exit_workflow.core.errors import ValidationError
from exit_workflow.core.money import ZERO, ensure_non_negative, quantize, subtract_floor_zero
from exit_workflow.domain.enums import DocumentType


# --------------------------------------------------------------------------
# Exit initiation
# --------------------------------------------------------------------------
def validate_move_out_date(move_out_date: date, today: date, settings: Settings) -> None:
    if move_out_date < today:
        raise ValidationError(
            "move_out_date cannot be in the past.",
            extra={"field": "move_out_date", "earliest": today.isoformat()},
        )
    earliest = today + timedelta(days=settings.min_notice_days)
    if move_out_date < earliest:
        raise ValidationError(
            f"move_out_date must be at least {settings.min_notice_days} day(s) away "
            f"(earliest {earliest.isoformat()}).",
            extra={"field": "move_out_date", "earliest": earliest.isoformat()},
        )
    latest = today + timedelta(days=settings.max_move_out_horizon_days)
    if move_out_date > latest:
        raise ValidationError(
            f"move_out_date cannot be more than {settings.max_move_out_horizon_days} "
            f"days ahead (latest {latest.isoformat()}).",
            extra={"field": "move_out_date", "latest": latest.isoformat()},
        )


def missing_required_documents(
    present: set[DocumentType], settings: Settings
) -> list[DocumentType]:
    required = {DocumentType(t) for t in settings.required_document_types}
    return sorted(required - present, key=lambda d: d.value)


# --------------------------------------------------------------------------
# Inspection scheduling (O15)
# --------------------------------------------------------------------------
def validate_slot(starts_at: datetime, ends_at: datetime, now: datetime, settings: Settings) -> None:
    if ends_at <= starts_at:
        raise ValidationError("Inspection slot must end after it starts.")
    if (ends_at - starts_at) > timedelta(hours=12):
        raise ValidationError("Inspection slot cannot span more than 12 hours.")
    lead = timedelta(hours=settings.inspection_slot_min_lead_hours)
    if starts_at < now + lead:
        raise ValidationError(
            f"Inspection slots must start at least {settings.inspection_slot_min_lead_hours}h "
            "from now.",
            extra={"earliest_start": (now + lead).isoformat()},
        )


# --------------------------------------------------------------------------
# Damage assessment and settlement (O16)
# --------------------------------------------------------------------------
def assessed_total(amounts: list[tuple[Decimal, bool]]) -> Decimal:
    """Sum of line items, counting only those the tenant is liable for."""

    total = sum((quantize(a) for a, liable in amounts if liable), start=ZERO)
    return quantize(total)


def validate_owner_adjustment(assessed: Decimal, final: Decimal) -> Decimal:
    """The owner may waive part of the assessed damage but never inflate it.

    The inspection agency is the independent valuer; letting an owner raise the
    deduction above the agency's assessment would make the third-party report
    meaningless and the deduction unappealable.
    """

    final = ensure_non_negative(final, "deduction_amount")
    if final > quantize(assessed):
        raise ValidationError(
            "Final deduction cannot exceed the amount assessed by the inspection agency.",
            extra={"assessed_amount": str(quantize(assessed)), "requested": str(final)},
        )
    return final


@dataclass(frozen=True, slots=True)
class SettlementBreakdown:
    security_deposit_amount: Decimal
    total_deduction_amount: Decimal
    refund_amount: Decimal
    balance_due_from_tenant: Decimal
    currency: str

    @property
    def requires_payout(self) -> bool:
        return self.refund_amount > ZERO


def compute_settlement(
    *,
    security_deposit_amount: Decimal,
    total_deduction_amount: Decimal,
    settings: Settings,
    currency: str | None = None,
) -> SettlementBreakdown:
    """O16: refund = deposit − damage, floored at zero.

    The SRS says "deposit minus damage" and is silent on damage exceeding the
    deposit. A negative refund is not payable, so the shortfall is recorded as
    ``balance_due_from_tenant`` for the owner to pursue outside this module;
    the exit itself is not held hostage to it.
    """

    deposit = ensure_non_negative(security_deposit_amount, "security_deposit_amount")
    deduction = ensure_non_negative(total_deduction_amount, "total_deduction_amount")

    if deduction > deposit and not settings.allow_deduction_above_deposit:
        raise ValidationError(
            "Total deduction exceeds the security deposit; adjust the deduction before "
            "settling.",
            extra={
                "security_deposit_amount": str(deposit),
                "total_deduction_amount": str(deduction),
            },
        )

    return SettlementBreakdown(
        security_deposit_amount=deposit,
        total_deduction_amount=deduction,
        refund_amount=subtract_floor_zero(deposit, deduction),
        balance_due_from_tenant=subtract_floor_zero(deduction, deposit),
        currency=(currency or settings.currency).upper(),
    )
