"""rules.yaml#EXIT-05 (stall) and the branches that stop rather than guess."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from exit_workflow.adapters.noc_pdf import SimpleNocPdfRenderer
from exit_workflow.clock import UTC, FrozenClock
from exit_workflow.db.models import AdminTask
from exit_workflow.enums import AdminTaskType, WorkflowState
from exit_workflow.errors import SpecUnresolved, WrongState
from exit_workflow.services.workflow import ExitWorkflowService

from ..support import initiate, status


def _service(session_factory, settings, clock, reasons, gateway, storage, publisher):
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


def test_workflow_stalls_30_days_past_move_out(
    service, session_factory, clock, tenant, move_out_date
):
    """rules.yaml#EXIT-05 — inspection must be scheduled within 30 days of move_out_date."""
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)

    # Day 30 exactly: still inside the window.
    clock.advance(timedelta(days=(move_out_date - date(2026, 3, 1)).days + 30))
    assert service.run_stall_sweep() == []
    assert status(session_factory, result.workflow_id) is WorkflowState.OWNER_NOTIFIED

    clock.advance(timedelta(days=1))
    assert service.run_stall_sweep() == [result.workflow_id]
    assert status(session_factory, result.workflow_id) is WorkflowState.STALLED

    with session_factory() as session:
        task = session.execute(select(AdminTask)).scalar_one()
        assert task.type == AdminTaskType.EXIT_WORKFLOW_STALLED
        assert task.workflow_id == result.workflow_id


def test_stall_applies_from_inspection_scheduled_too(
    service, session_factory, clock, tenant, owner, move_out_date
):
    """states.yaml lists STALLED from OWNER_NOTIFIED and from INSPECTION_SCHEDULED."""
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)
    service.schedule_inspection(result.workflow_id, principal=owner)

    clock.advance(timedelta(days=90))
    assert service.run_stall_sweep() == [result.workflow_id]
    assert status(session_factory, result.workflow_id) is WorkflowState.STALLED


def test_stall_sweep_is_idempotent(service, session_factory, clock, tenant, move_out_date):
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)
    clock.advance(timedelta(days=90))

    assert service.run_stall_sweep() == [result.workflow_id]
    assert service.run_stall_sweep() == []
    with session_factory() as session:
        assert len(session.execute(select(AdminTask)).scalars().all()) == 1


def test_stalled_workflow_does_not_auto_cancel_and_cannot_complete(
    service, session_factory, clock, tenant, owner, system, move_out_date
):
    """rules.yaml#EXIT-05 — "It does not auto-cancel." states.yaml forbids STALLED -> COMPLETE.

    states.yaml defines no transition out of STALLED at all, so every onward call is
    refused. Whether and how an admin resumes a stalled workflow is an open question —
    blockers.md#B-13.
    """
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)
    clock.advance(timedelta(days=90))
    service.run_stall_sweep()

    with pytest.raises(WrongState):
        service.schedule_inspection(result.workflow_id, principal=owner)
    with pytest.raises(WrongState):
        service.settle(result.workflow_id, principal=system)
    assert status(session_factory, result.workflow_id) is WorkflowState.STALLED


def test_completed_workflow_is_never_stalled(
    service, session_factory, clock, tenant, owner, agency, system, move_out_date
):
    from ..support import drive_to_damage_confirmed

    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("1.00"),
    )
    service.settle(workflow_id, principal=system)
    clock.advance(timedelta(days=365))
    assert service.run_stall_sweep() == []
    assert status(session_factory, workflow_id) is WorkflowState.COMPLETE


def test_owner_dispute_is_blocked(service, tenant, owner, agency, move_out_date):
    """rules.yaml#EXIT-06 mentions a dispute; states.yaml and api.yaml define none.

    BLOCKED, blockers.md#B-1.
    """
    from ..support import drive_to_damage_confirmed

    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("1.00"),
    )
    with pytest.raises(SpecUnresolved) as raised:
        service.dispute_damage(workflow_id, principal=owner)
    assert raised.value.blocker == "B-1"
    # api.yaml declares no code for this branch, so none is invented.
    assert raised.value.code is None


def test_no_dispute_route_is_exposed(app):
    """api.yaml declares no dispute path, so the module declares none."""
    paths = set(app.openapi()["paths"])
    assert not any("dispute" in path for path in paths)
    assert paths == {
        "/exit-workflows",
        "/exit-workflows/{workflow_id}/schedule-inspection",
        "/exit-workflows/{workflow_id}/inspection-report",
        "/exit-workflows/{workflow_id}/confirm-damage",
        "/exit-workflows/{workflow_id}/settle",
    }


def test_recover_unnotified_advances_orphaned_workflows(
    service, session_factory, publisher, tenant, move_out_date
):
    """A crash between the initiation commit and the notify step is recoverable."""
    result = initiate(service, tenant, move_out_date)
    assert status(session_factory, result.workflow_id) is WorkflowState.DOCS_SUBMITTED

    assert service.recover_unnotified() == [result.workflow_id]
    assert status(session_factory, result.workflow_id) is WorkflowState.OWNER_NOTIFIED
    assert [event.key for event in publisher.published] == [result.workflow_id]


def test_workflow_id_sequence_overflow_is_blocked(
    session_factory, settings, reasons, gateway, storage, publisher, seed, tenant
):
    """rules.yaml#EXIT-02 fixes NNNNN at five digits and does not say what follows.

    blockers.md#B-4.
    """
    import sqlalchemy as sa

    from exit_workflow.services.ids import next_workflow_id

    with session_factory() as session:
        session.execute(sa.text("SELECT setval('exit_workflow_id_seq', 99999, true)"))
        session.commit()
        with pytest.raises(SpecUnresolved) as raised:
            next_workflow_id(session, date(2026, 3, 1))
    assert raised.value.blocker == "B-4"


def test_reference_list_cannot_be_empty():
    """blockers.md#B-2 — an empty list would disguise missing reference data."""
    from exit_workflow.ports.reference import StaticExitReasonReference

    with pytest.raises(ValueError):
        StaticExitReasonReference(frozenset())


def test_frozen_clock_requires_awareness():
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 1, 1))
    assert FrozenClock(datetime(2026, 1, 1, tzinfo=UTC)).now_utc().tzinfo is not None
