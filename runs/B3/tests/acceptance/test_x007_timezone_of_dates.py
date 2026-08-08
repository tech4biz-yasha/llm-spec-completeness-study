"""edges.yaml#X-007 — timezone of dates. Decision D-001, Asia/Dubai."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
import sqlalchemy as sa

from exit_workflow.clock import UTC, FrozenClock, business_date, business_today
from exit_workflow.errors import MoveOutDateInPast
from exit_workflow.services.workflow import ExitWorkflowService

from ..support import initiate


def _service(session_factory, settings, clock, reasons, gateway, storage, publisher):
    from exit_workflow.adapters.noc_pdf import SimpleNocPdfRenderer

    return ExitWorkflowService(
        session_factory=session_factory,
        settings=settings,
        clock=clock,
        reasons=reasons,
        gateway=gateway,
        storage=storage,
        renderer=SimpleNocPdfRenderer(),
        publisher=publisher,
    )


def test_x007(session_factory, settings, reasons, gateway, storage, publisher, seed, tenant):
    """Calendar day in Asia/Dubai; comparisons use the Dubai calendar.

    At 21:00 UTC on 2026-03-01 it is already 2026-03-02 in Dubai (UTC+4). A move-out date
    of 2026-03-02 must therefore still be accepted as "today", and 2026-03-01 must be
    rejected as past — the opposite of what a UTC comparison would conclude.
    """
    clock = FrozenClock(datetime(2026, 3, 1, 21, 0, tzinfo=UTC))
    assert business_today(clock) == date(2026, 3, 2)

    service = _service(session_factory, settings, clock, reasons, gateway, storage, publisher)

    # 2026-03-01 is still "today" in UTC but yesterday in Dubai, so it is rejected.
    with pytest.raises(MoveOutDateInPast) as raised:
        service.initiate(
            principal=tenant,
            contract_id="CON-1",
            move_out_date=date(2026, 3, 1),
            reason="END_OF_TENANCY",
            documents=[{"id": "DOC-1"}],
        )
    assert raised.value.code == "MOVE_OUT_DATE_IN_PAST"
    assert raised.value.details["today_asia_dubai"] == "2026-03-02"

    # 2026-03-02 is today in Dubai, so it is accepted, and dates the workflow ID.
    result = initiate(service, tenant, date(2026, 3, 2))
    assert result.workflow_id.startswith("EX-20260302-")


def test_x007_move_out_date_is_stored_as_a_date_not_a_datetime(
    service, session_factory, engine, tenant, move_out_date
):
    result = initiate(service, tenant, move_out_date)
    with session_factory() as session:
        stored = session.execute(
            sa.text("SELECT move_out_date FROM exit_workflows WHERE id = :id"),
            {"id": result.workflow_id},
        ).scalar_one()
    assert isinstance(stored, date) and not isinstance(stored, datetime)
    assert stored == move_out_date

    column_type = sa.inspect(engine).get_columns("exit_workflows")
    move_out = next(c for c in column_type if c["name"] == "move_out_date")
    assert isinstance(move_out["type"], sa.Date)


def test_x007_timestamps_are_stored_utc(service, session_factory, tenant, move_out_date):
    """AGENTS.md — timestamps stored UTC, while the business day is Dubai."""
    result = initiate(service, tenant, move_out_date)
    with session_factory() as session:
        created_at = session.execute(
            sa.text("SELECT created_at FROM exit_workflows WHERE id = :id"),
            {"id": result.workflow_id},
        ).scalar_one()
    assert created_at.tzinfo is not None
    assert created_at.astimezone(UTC) == datetime(2026, 3, 1, 8, 0, tzinfo=UTC)


def test_x007_business_date_rejects_naive_datetimes():
    with pytest.raises(ValueError):
        business_date(datetime(2026, 3, 1, 12, 0))


def test_x007_today_is_accepted(service, session_factory, clock, tenant):
    """rules.yaml#EXIT-02 — move_out_date is "today or later"."""
    today = business_today(clock)
    result = initiate(service, tenant, today)
    assert result.workflow_id
    assert today + timedelta(days=0) == today
