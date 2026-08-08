"""Allocation of human-facing reference numbers."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sequences import NOC_NUMBER_SEQ, WORKFLOW_REFERENCE_SEQ


async def next_workflow_reference(session: AsyncSession) -> str:
    """T13 step 5 — Workflow ID generation, e.g. ``EXW-2026-000001``."""
    value = await session.scalar(sa.select(WORKFLOW_REFERENCE_SEQ.next_value()))
    return f"EXW-{datetime.now(UTC).year}-{int(value):06d}"


async def next_noc_number(session: AsyncSession) -> str:
    """T13 step 10 — Exit NOC number, e.g. ``NOC-2026-000042``."""
    value = await session.scalar(sa.select(NOC_NUMBER_SEQ.next_value()))
    return f"NOC-{datetime.now(UTC).year}-{int(value):06d}"
