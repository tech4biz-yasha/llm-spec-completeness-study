"""Workflow identifiers.

rules.yaml#EXIT-02: "Workflow ID format EX-YYYYMMDD-NNNNN, server assigned,
sequence from PostgreSQL."

The date part is the Asia/Dubai calendar day of assignment (decision D-001), so
an exit opened at 02:00 Dubai carries that Dubai day, not the previous UTC one.
The counter is drawn from a PostgreSQL sequence; see
:func:`exit_workflow.repositories.workflow.WorkflowRepository.next_workflow_id`.

Two properties of NNNNN the kit does not fix are recorded in blockers.md#B-3:
whether the counter resets per day, and what happens at 100 000. This module
uses a single monotonic sequence (the reading that makes the ID unique without a
daily reset job) and refuses to emit a malformed ID on overflow.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Final

from exit_workflow.domain.errors import SpecUnresolved

SEQUENCE_NAME: Final[str] = "exit_workflow_number_seq"
SEQUENCE_WIDTH: Final[int] = 5
MAX_SEQUENCE: Final[int] = 10**SEQUENCE_WIDTH - 1

WORKFLOW_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^EX-\d{8}-\d{5}$")


def format_workflow_id(assigned_on: date, sequence: int) -> str:
    """Render ``EX-YYYYMMDD-NNNNN`` (rules.yaml#EXIT-02).

    :param assigned_on: Asia/Dubai calendar day of assignment.
    :param sequence: value drawn from the PostgreSQL sequence.
    """
    if sequence < 1:
        raise ValueError(f"sequence value {sequence} is not positive")
    if sequence > MAX_SEQUENCE:
        # rules.yaml#EXIT-02 fixes the width at five digits and says nothing
        # about exhaustion. Widening the field or wrapping the counter would
        # both be inventions, and either could collide with an issued ID.
        raise SpecUnresolved(
            "B-3",
            f"Workflow ID sequence reached {sequence}, beyond the {SEQUENCE_WIDTH}-digit "
            "NNNNN field fixed by rules.yaml#EXIT-02. Exhaustion behaviour is undecided.",
            sequence=sequence,
            max_sequence=MAX_SEQUENCE,
        )
    return f"EX-{assigned_on:%Y%m%d}-{sequence:0{SEQUENCE_WIDTH}d}"


def is_workflow_id(value: str) -> bool:
    """True when ``value`` has the shape rules.yaml#EXIT-02 defines."""
    return bool(WORKFLOW_ID_PATTERN.match(value))
