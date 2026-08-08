"""edges.yaml#X-002 — owner notification dispatch fails. rules.yaml#EXIT-04."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from exit_workflow.adapters.events import FailingEventPublisher
from exit_workflow.adapters.noc_pdf import SimpleNocPdfRenderer
from exit_workflow.db.models import AdminTask, ExitWorkflow, OutboxEvent, Property
from exit_workflow.enums import AdminTaskType, OutboxStatus, WorkflowState
from exit_workflow.services.workflow import ExitWorkflowService

from ..conftest import PROPERTY_ID
from ..support import initiate, status


def _service_with(publisher, session_factory, settings, clock, reasons, gateway, storage):
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


def test_x002(
    session_factory, settings, clock, reasons, gateway, storage, seed, tenant, move_out_date
):
    """Workflow never rolls back; the event retries 5x with backoff, then dead-letters."""
    publisher = FailingEventPublisher()
    service = _service_with(publisher, session_factory, settings, clock, reasons, gateway, storage)

    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)

    # The workflow committed and advanced. Dispatch failure changed nothing about it.
    assert status(session_factory, result.workflow_id) is WorkflowState.OWNER_NOTIFIED
    with session_factory() as session:
        assert session.get(ExitWorkflow, result.workflow_id) is not None
        # rules.yaml#EXIT-03 — the exit lock stands.
        assert session.get(Property, PROPERTY_ID).exit_lock is True
        event = session.get(OutboxEvent, result.outbox_event_id)
        assert event.status == OutboxStatus.PENDING
        assert event.attempts == 1
        assert "kafka unreachable" in event.last_error

    # rules.yaml#EXIT-04 — exponential backoff across 5 attempts, then dead-letter.
    for expected_attempt in range(2, settings.notification_max_attempts + 1):
        clock.advance(timedelta(hours=2))
        service.dispatch_pending_notifications()
        with session_factory() as session:
            event = session.get(OutboxEvent, result.outbox_event_id)
            assert event.attempts == expected_attempt

    assert publisher.attempts == settings.notification_max_attempts

    with session_factory() as session:
        event = session.get(OutboxEvent, result.outbox_event_id)
        assert event.status == OutboxStatus.DEAD_LETTER
        # rules.yaml#EXIT-04 — dead-letter plus admin alert.
        task = session.execute(select(AdminTask)).scalar_one()
        assert task.type == AdminTaskType.OWNER_NOTIFICATION_DEAD_LETTER
        assert task.workflow_id == result.workflow_id

    # And still: the workflow is intact.
    assert status(session_factory, result.workflow_id) is WorkflowState.OWNER_NOTIFIED


def test_x002_backoff_grows_exponentially(
    session_factory, settings, clock, reasons, gateway, storage, seed, tenant, move_out_date
):
    publisher = FailingEventPublisher()
    service = _service_with(publisher, session_factory, settings, clock, reasons, gateway, storage)
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)

    delays = []
    for _ in range(3):
        with session_factory() as session:
            event = session.get(OutboxEvent, result.outbox_event_id)
            delays.append((event.next_attempt_at - clock.now_utc()).total_seconds())
        clock.advance(timedelta(hours=2))
        service.dispatch_pending_notifications()

    assert delays == sorted(delays)
    assert delays[1] == delays[0] * 2
    assert delays[2] == delays[1] * 2


def test_x002_recovers_when_dispatch_later_succeeds(
    session_factory,
    settings,
    clock,
    reasons,
    gateway,
    storage,
    publisher,
    seed,
    tenant,
    move_out_date,
):
    failing = FailingEventPublisher()
    service = _service_with(failing, session_factory, settings, clock, reasons, gateway, storage)
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)

    working = _service_with(publisher, session_factory, settings, clock, reasons, gateway, storage)
    clock.advance(timedelta(hours=2))
    outcome = working.dispatch_pending_notifications()

    assert outcome.sent == 1
    assert [event.key for event in publisher.published] == [result.workflow_id]
    with session_factory() as session:
        assert session.get(OutboxEvent, result.outbox_event_id).status == OutboxStatus.SENT
