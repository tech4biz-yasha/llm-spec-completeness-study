"""Server-assigned identifiers.

rules.yaml#EXIT-02: "Workflow ID format EX-YYYYMMDD-NNNNN, server assigned, sequence from
PostgreSQL."
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..errors import SpecUnresolved

WORKFLOW_ID_SEQUENCE = "exit_workflow_id_seq"
_MAX_SEQUENCE_VALUE = 99_999  # NNNNN


def next_workflow_id(session: Session, business_day: date) -> str:
    """EX-YYYYMMDD-NNNNN, with NNNNN drawn from the PostgreSQL sequence.

    rules.yaml#EXIT-02. The kit does not say whether the counter resets each day, and a
    plain sequence overruns the five-digit field at 100 000 workflows. Rather than
    silently emitting an ID that no longer matches the specified format, that case stops:
    blockers.md#B-4.
    """
    value = session.execute(text(f"SELECT nextval('{WORKFLOW_ID_SEQUENCE}')")).scalar_one()
    if value > _MAX_SEQUENCE_VALUE:
        raise SpecUnresolved(
            "B-4",
            "workflow ID sequence exceeded NNNNN; rules.yaml#EXIT-02 does not say "
            "whether the sequence resets per day",
            sequence_value=int(value),
        )
    return f"EX-{business_day:%Y%m%d}-{value:05d}"


def new_payment_id() -> str:
    return f"PAY-{uuid.uuid4()}"


def new_noc_id() -> str:
    return f"NOC-{uuid.uuid4()}"


def new_event_id() -> str:
    return f"EVT-{uuid.uuid4()}"


def new_admin_task_id() -> str:
    return f"TASK-{uuid.uuid4()}"
