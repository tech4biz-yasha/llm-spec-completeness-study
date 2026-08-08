"""Pure business rules. No I/O, no ORM, no framework.

Every function here cites the rule it implements (AGENTS.md, "every function
implementing a rule cites its ID").
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from ..errors import (
    DocumentsRequired,
    MoveOutDateInPast,
    ReasonInvalid,
    SpecUnresolved,
)
from ..money import from_minor, to_minor
from .states import State

#: rules.yaml#EXIT-02 — "Workflow ID format EX-YYYYMMDD-NNNNN, server assigned,
#: sequence from PostgreSQL."
WORKFLOW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^EX-\d{8}-\d{5}$")
_SEQUENCE_DIGITS: Final[int] = 5

#: rules.yaml#EXIT-05 — "Inspection must be scheduled within 30 days of move_out_date."
STALL_WINDOW: Final[timedelta] = timedelta(days=30)

#: states.yaml — the states from which the 30-day timer moves a workflow to STALLED.
STALLABLE_STATES: Final[frozenset[State]] = frozenset(
    {State.OWNER_NOTIFIED, State.INSPECTION_SCHEDULED}
)


def format_workflow_id(dubai_day: date, sequence_value: int) -> str:
    """rules.yaml#EXIT-02 — EX-YYYYMMDD-NNNNN.

    The date part is the Asia/Dubai calendar day (D-001, edges.yaml#X-007); the
    NNNNN part is a PostgreSQL sequence value, zero padded to 5 digits.

    blockers.md#B-008: the spec does not say whether the sequence resets daily,
    nor what happens past 99999 in one namespace. This module uses one global
    sequence and refuses to emit a malformed id rather than truncate.
    """
    if sequence_value < 0:
        raise ValueError("sequence value must be non-negative")
    body = str(sequence_value).zfill(_SEQUENCE_DIGITS)
    if len(body) > _SEQUENCE_DIGITS:
        # Truncating or wrapping would mint a duplicate id. Stop instead.
        raise SpecUnresolved(
            "B-008",
            "workflow id sequence exceeded 99999 and the spec defines no rollover "
            "(rules.yaml#EXIT-02)",
        )
    return f"EX-{dubai_day.strftime('%Y%m%d')}-{body}"


def validate_move_out_date(move_out_date: date, today_dubai: date) -> None:
    """rules.yaml#EXIT-02, edges.yaml#X-007 — today or later, Dubai calendar."""
    if move_out_date < today_dubai:
        raise MoveOutDateInPast(
            f"move_out_date {move_out_date.isoformat()} is before today "
            f"{today_dubai.isoformat()} in Asia/Dubai"
        )


def validate_reason(reason: str, allowed_reasons: Collection[str] | None) -> None:
    """rules.yaml#EXIT-02 — "a reason from the reference list".

    The reference list itself is an open item: risks.md, "Open items also flagged
    in the SRS itself" — *Reference data dictionary, specifically exit reasons
    (blocks the ExitWorkflow enum)*. This module therefore validates against an
    injected reference list and never hard-codes one. With no list configured
    the branch is BLOCKED (blockers.md#B-001).
    """
    if allowed_reasons is None:
        raise SpecUnresolved(
            "B-001",
            "exit reason reference list is not defined (risks.md Appendix A: "
            "reference data dictionary); cannot validate rules.yaml#EXIT-02",
        )
    if reason not in allowed_reasons:
        raise ReasonInvalid(f"reason {reason!r} is not in the exit reason reference list")


def validate_documents(documents: Sequence[object]) -> None:
    """rules.yaml#EXIT-02 — at least one document."""
    if len(documents) < 1:
        raise DocumentsRequired("at least one document is required to initiate an exit")


def refund_minor(security_deposit_minor: int, confirmed_damage_minor: int) -> int:
    """rules.yaml#EXIT-07 — refund = max(deposit - damage, 0), Decimal, half-up 2 dp.

    edges.yaml#X-003 / risks.md#R8: when confirmed_damage > security_deposit the
    behaviour is UNDECIDED. Raise SpecUnresolved. Do not cap-and-write-off, do
    not create a debt record. No refund, no NOC; the workflow holds at
    DAMAGE_CONFIRMED.
    """
    if confirmed_damage_minor > security_deposit_minor:
        # rules.yaml#EXIT-07 — BLOCKED until R8 is decided.
        raise SpecUnresolved(
            "R8",
            "confirmed damage exceeds the security deposit; behaviour undecided "
            "(risks.md#R8, edges.yaml#X-003)",
            details={
                "security_deposit": str(from_minor(security_deposit_minor)),
                "confirmed_damage": str(from_minor(confirmed_damage_minor)),
            },
        )
    # Decimal arithmetic, half-up at 2 dp. Both operands are exact minor units,
    # so the quantisation in to_minor() is a no-op here — it is applied anyway so
    # the rule is expressed as written, not as an integer shortcut.
    refund: Decimal = from_minor(security_deposit_minor) - from_minor(confirmed_damage_minor)
    return max(to_minor(refund), 0)


def stall_deadline(move_out_date: date) -> date:
    """rules.yaml#EXIT-05 — last Dubai calendar day within the 30-day window."""
    return move_out_date + STALL_WINDOW


def is_past_stall_window(move_out_date: date, today_dubai: date) -> bool:
    """rules.yaml#EXIT-05, states.yaml `when: 30_days_past_move_out`.

    True once the Dubai calendar day is past move_out_date + 30 days.
    """
    return today_dubai > stall_deadline(move_out_date)
