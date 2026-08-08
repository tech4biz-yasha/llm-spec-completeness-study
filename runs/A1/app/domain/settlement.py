"""Deposit settlement arithmetic (SRS O16).

Pure integer arithmetic on fils. The rule the SRS states is *refund = deposit - damage*;
the SRS is silent on what happens when damage exceeds the deposit, so this module clamps
the refund at zero and surfaces the excess as a ``balance_due`` owed by the tenant. A
settlement is only closable once both legs — the owner's refund and the tenant's balance —
are satisfied, which is what gates NOC issuance.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.errors import ValidationError
from app.money import format_aed


@dataclass(frozen=True, slots=True)
class DeductionInput:
    """A single damage line item proposed by the inspection agency."""

    code: str
    description: str
    amount_fils: int

    def __post_init__(self) -> None:
        if self.amount_fils < 0:
            raise ValidationError(
                "deduction amount cannot be negative",
                details={"code": self.code, "amount_fils": self.amount_fils},
            )


@dataclass(frozen=True, slots=True)
class SettlementBreakdown:
    """The computed outcome of a settlement. All amounts are non-negative fils."""

    deposit_fils: int
    total_deductions_fils: int
    refund_fils: int
    balance_due_fils: int

    @property
    def tenant_owes(self) -> bool:
        return self.balance_due_fils > 0

    @property
    def is_fully_deducted(self) -> bool:
        return self.refund_fils == 0

    def as_dict(self) -> dict[str, int]:
        return {
            "deposit_fils": self.deposit_fils,
            "total_deductions_fils": self.total_deductions_fils,
            "refund_fils": self.refund_fils,
            "balance_due_fils": self.balance_due_fils,
        }

    def as_display(self) -> dict[str, str]:
        return {
            "deposit": format_aed(self.deposit_fils),
            "total_deductions": format_aed(self.total_deductions_fils),
            "refund": format_aed(self.refund_fils),
            "balance_due": format_aed(self.balance_due_fils),
        }


def total_deductions(deductions: Iterable[DeductionInput]) -> int:
    return sum(d.amount_fils for d in deductions)


def compute_settlement(
    *, deposit_fils: int, deductions: Sequence[DeductionInput]
) -> SettlementBreakdown:
    """Apply O16: refund = deposit - damages, floored at zero, excess becomes balance due.

    Raises:
        ValidationError: if the deposit is negative.
    """
    if deposit_fils < 0:
        raise ValidationError(
            "security deposit cannot be negative", details={"deposit_fils": deposit_fils}
        )

    deducted = total_deductions(deductions)
    net = deposit_fils - deducted
    refund = max(net, 0)
    balance_due = max(-net, 0)

    # Invariant: exactly one of refund/balance_due is non-zero, and the books balance.
    assert refund - balance_due == deposit_fils - deducted
    assert refund == 0 or balance_due == 0

    return SettlementBreakdown(
        deposit_fils=deposit_fils,
        total_deductions_fils=deducted,
        refund_fils=refund,
        balance_due_fils=balance_due,
    )
