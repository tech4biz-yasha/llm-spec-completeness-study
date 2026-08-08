"""One test per entry in edges.yaml, named as its ``test`` field."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from exit_workflow.api.security import HeaderPrincipalResolver
from exit_workflow.app import create_app
from exit_workflow.domain.clock import DUBAI, FixedClock
from exit_workflow.domain.enums import OutboxStatus, PaymentStatus
from exit_workflow.domain.errors import ExitWorkflowIncomplete
from exit_workflow.domain.reasons import ConfiguredExitReasons
from exit_workflow.domain.states import State
from exit_workflow.services.guards import assert_property_contractable
from tests import helpers
from tests.conftest import FIXTURE_REASONS


async def test_x001(client, tenancy, initiate_body, session_factory):
    """X-001 duplicate_initiation.

    "Tenant initiates exit twice on the same contract -> 409
    EXIT_ALREADY_IN_PROGRESS with existing workflow_id. Never a second workflow."
    """
    first = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    assert first.status_code == 201
    workflow_id = first.json()["workflow_id"]

    second = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "EXIT_ALREADY_IN_PROGRESS"
    assert error["details"]["workflow_id"] == workflow_id

    count = await helpers.scalar(
        session_factory,
        "SELECT count(*) FROM exit_workflows WHERE contract_id = :contract_id",
        contract_id=tenancy.contract_id,
    )
    assert count == 1


async def test_x001_concurrent(client, tenancy, initiate_body, session_factory):
    """X-001 under a race: two simultaneous initiations still yield one workflow."""
    responses = await asyncio.gather(
        client.post("/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")),
        client.post("/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")),
    )
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409]

    count = await helpers.scalar(
        session_factory,
        "SELECT count(*) FROM exit_workflows WHERE contract_id = :contract_id",
        contract_id=tenancy.contract_id,
    )
    assert count == 1


async def test_x002(app, client, tenancy, initiate_body, publisher, session_factory):
    """X-002 notification_dispatch_fails.

    "Workflow stays DOCS_SUBMITTED->OWNER_NOTIFIED path intact; event retries 5x
    backoff then dead-letters. Workflow NEVER rolls back."
    """
    publisher.fail = True

    response = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    # rules.yaml#EXIT-04 — initiation succeeds regardless of the broker.
    assert response.status_code == 201
    workflow_id = response.json()["workflow_id"]

    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.DOCS_SUBMITTED
    # The exit lock committed with the workflow (rules.yaml#EXIT-03).
    lock = await helpers.scalar(
        session_factory,
        "SELECT exit_lock FROM properties WHERE id = :id",
        id=tenancy.property_id,
    )
    assert lock is True

    events = await helpers.rows(session_factory, "SELECT * FROM event_outbox")
    assert len(events) == 1
    assert events[0]["attempts"] == 1
    assert events[0]["status"] == OutboxStatus.PENDING

    # Attempts 2 to 5, then dead-letter plus admin alert.
    for _ in range(4):
        await app.state.dispatcher.dispatch_due()

    events = await helpers.rows(session_factory, "SELECT * FROM event_outbox")
    assert events[0]["attempts"] == 5
    assert events[0]["status"] == OutboxStatus.DEAD_LETTER

    tasks = await helpers.rows(
        session_factory, "SELECT * FROM admin_tasks WHERE workflow_id = :id", id=workflow_id
    )
    assert [task["task_type"] for task in tasks] == ["NOTIFICATION_DEAD_LETTER"]

    # The workflow never rolled back and never advanced past DOCS_SUBMITTED.
    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.DOCS_SUBMITTED

    # Once the broker recovers the queue drains and the workflow advances.
    # An admin re-queues the dead-lettered event once the broker is back.
    publisher.fail = False
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE event_outbox SET status = 'PENDING' WHERE id = :id"),
                {"id": events[0]["id"]},
            )
    await app.state.dispatcher.dispatch_due()
    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.OWNER_NOTIFIED


async def test_x003(client, tenancy, initiate_body, session_factory, noc_storage):
    """X-003 damage_exceeds_deposit.

    "raise SpecUnresolved(\"R8\"). No refund, no NOC, workflow holds at
    DAMAGE_CONFIRMED."
    """
    over_deposit = tenancy.deposit + Decimal("0.01")
    workflow_id = await helpers.drive_to_damage_confirmed(
        client, tenancy, initiate_body, damage=over_deposit
    )

    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.status_code == 501
    error = response.json()["error"]
    assert error["code"] == "SPEC_UNRESOLVED_R8"
    assert error["blocker"] == "R8"

    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.DAMAGE_CONFIRMED
    assert workflow["refund_amount_minor"] is None
    assert workflow["payment_id"] is None
    assert workflow["noc_document_id"] is None

    assert await helpers.scalar(session_factory, "SELECT count(*) FROM payments") == 0
    assert await helpers.scalar(session_factory, "SELECT count(*) FROM noc_documents") == 0
    assert await noc_storage.get(f"exit-workflows/{workflow_id}/noc.pdf") is None


async def test_x004(client, tenancy, initiate_body, gateway, session_factory, noc_storage):
    """X-004 gateway_pending_at_noc_time.

    "Refund payment still PENDING, NOC generation attempted -> Refuse. NOC only
    after SUCCEEDED."
    """
    gateway.status = PaymentStatus.PENDING
    workflow_id = await helpers.drive_to_damage_confirmed(
        client, tenancy, initiate_body, damage=Decimal("500.00")
    )

    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PAYMENT_PENDING"

    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.DAMAGE_CONFIRMED
    assert workflow["noc_document_id"] is None
    assert await helpers.scalar(session_factory, "SELECT count(*) FROM noc_documents") == 0
    assert await noc_storage.get(f"exit-workflows/{workflow_id}/noc.pdf") is None

    # The payment record survives the hold, so the refund is not submitted twice.
    payments = await helpers.rows(session_factory, "SELECT * FROM payments")
    assert len(payments) == 1
    assert payments[0]["status"] == PaymentStatus.PENDING
    assert payments[0]["idempotency_key"] == workflow_id

    # Gateway confirms: settlement resumes from where it stopped.
    gateway.status = PaymentStatus.SUCCEEDED
    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.status_code == 200
    assert response.json()["status"] == State.COMPLETE
    assert len(await helpers.rows(session_factory, "SELECT * FROM payments")) == 1


async def test_x005(client, tenancy, initiate_body, session_factory, gateway):
    """X-005 concurrent_settlement.

    "Idempotency key = workflow_id means one payment, second call returns
    existing."
    """
    workflow_id = await helpers.drive_to_damage_confirmed(
        client, tenancy, initiate_body, damage=Decimal("1500.00")
    )

    first, second = await asyncio.gather(
        helpers.settle(client, tenancy, workflow_id),
        helpers.settle(client, tenancy, workflow_id),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    payments = await helpers.rows(session_factory, "SELECT * FROM payments")
    assert len(payments) == 1
    assert payments[0]["idempotency_key"] == workflow_id
    # One submission to the gateway, not two.
    assert len(gateway.submissions) == 1

    for response in (first, second):
        body = response.json()
        assert body["payment_id"] == str(payments[0]["id"])
        assert body["refund_amount"] == "10500.00"
        assert body["status"] == State.COMPLETE

    # And exactly one NOC.
    assert await helpers.scalar(session_factory, "SELECT count(*) FROM noc_documents") == 1


async def test_x006(client, tenancy, initiate_body, session_factory):
    """X-006 exit_lock_vs_new_contract.

    "New contract attempted on the property mid-exit -> 409
    EXIT_WORKFLOW_INCOMPLETE."
    """
    workflow_id = await helpers.initiate(client, tenancy, initiate_body)

    async with session_factory() as session:
        with pytest.raises(ExitWorkflowIncomplete) as raised:
            await assert_property_contractable(session, tenancy.property_id)

    assert raised.value.code == "EXIT_WORKFLOW_INCOMPLETE"
    assert raised.value.http_status == 409
    assert raised.value.details["workflow_id"] == workflow_id

    # rules.yaml#EXIT-03 — the lock is released only by COMPLETE. Drive the
    # same workflow through: a second one on this contract is impossible.
    await helpers.advance_to_damage_confirmed(
        client, tenancy, workflow_id, damage=Decimal("0.00")
    )
    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.status_code == 200
    assert response.json()["status"] == State.COMPLETE

    async with session_factory() as session:
        await assert_property_contractable(session, tenancy.property_id)  # no longer raises


async def test_x007(settings, session_factory, publisher, gateway, noc_storage, tenancy):
    """X-007 timezone_of_dates.

    "Calendar day in Asia/Dubai, stored as date not datetime. Comparisons use
    Dubai calendar." (D-001)

    The clock is set to 21:00 UTC, which is already the next day in Dubai. The
    Dubai day must be accepted and the UTC day — yesterday in Dubai — refused.
    """
    instant = datetime(2026, 8, 8, 21, 0, tzinfo=UTC)
    dubai_today = instant.astimezone(DUBAI).date()
    utc_today = instant.date()
    assert dubai_today != utc_today  # the whole point of the test

    app = create_app(
        settings=settings,
        session_factory=session_factory,
        principal_resolver=HeaderPrincipalResolver(allow=True),
        publisher=publisher,
        payment_gateway=gateway,
        noc_storage=noc_storage,
        exit_reasons=ConfiguredExitReasons(settings.exit_reason_codes),
        clock=FixedClock(instant),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://exit-workflow.test") as client:
        # The UTC calendar day is yesterday in Dubai: refused.
        response = await client.post(
            "/exit-workflows",
            json={
                "contract_id": str(tenancy.contract_id),
                "move_out_date": utc_today.isoformat(),
                "reason": FIXTURE_REASONS[0],
                "documents": ["doc-ref-1"],
            },
            headers=tenancy.headers("tenant"),
        )
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "MOVE_OUT_DATE_IN_PAST"
        assert error["details"]["today_asia_dubai"] == dubai_today.isoformat()

        # Today in Dubai: accepted.
        response = await client.post(
            "/exit-workflows",
            json={
                "contract_id": str(tenancy.contract_id),
                "move_out_date": dubai_today.isoformat(),
                "reason": FIXTURE_REASONS[0],
                "documents": ["doc-ref-1"],
            },
            headers=tenancy.headers("tenant"),
        )
        assert response.status_code == 201, response.text
        workflow_id = response.json()["workflow_id"]

    # Stored as a date, and the ID carries the Dubai day (rules.yaml#EXIT-02).
    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["move_out_date"] == dubai_today
    assert workflow_id.startswith(f"EX-{dubai_today:%Y%m%d}-")


async def test_x007_stall_window_uses_dubai_days(
    settings, session_factory, publisher, gateway, noc_storage, tenancy, client, initiate_body
):
    """rules.yaml#EXIT-05 counted in Dubai calendar days: day 30 is still inside."""
    from exit_workflow.services.stall import run_stall_scan

    workflow_id = await helpers.initiate(client, tenancy, initiate_body)
    response = await helpers.schedule_inspection(client, tenancy, workflow_id)
    assert response.status_code == 200

    move_out = (await helpers.workflow_row(session_factory, workflow_id))["move_out_date"]

    # Exactly 30 days past move-out: within the window, no stall.
    at_threshold = datetime.combine(move_out + timedelta(days=30), datetime.min.time())
    clock = FixedClock(at_threshold.replace(hour=12, tzinfo=DUBAI))
    report = await run_stall_scan(session_factory, clock=clock)
    assert report.stalled == []

    # One day later: stalled, with an admin task.
    clock = FixedClock((at_threshold + timedelta(days=1)).replace(hour=12, tzinfo=DUBAI))
    report = await run_stall_scan(session_factory, clock=clock)
    assert report.stalled == [workflow_id]

    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.STALLED
    tasks = await helpers.rows(
        session_factory, "SELECT * FROM admin_tasks WHERE workflow_id = :id", id=workflow_id
    )
    assert [task["task_type"] for task in tasks] == ["EXIT_STALLED"]
