"""Database sequences backing human-facing reference numbers.

Attached to ``Base.metadata`` so ``create_all`` and Alembic both manage them. Using a real
sequence (rather than ``MAX(reference) + 1``) means allocation is atomic, lock-free and safe
across replicas. Counters are global rather than per-year, so a reference is unique for all
time even though the year is embedded for readability.
"""

from __future__ import annotations

import sqlalchemy as sa

from app.models.base import Base

WORKFLOW_REFERENCE_SEQ = sa.Sequence(
    "exit_workflow_reference_seq", metadata=Base.metadata, start=1, increment=1
)
NOC_NUMBER_SEQ = sa.Sequence("exit_noc_number_seq", metadata=Base.metadata, start=1, increment=1)
