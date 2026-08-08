"""edges.yaml — one test per case, named as the file names them."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from exit_workflow.errors import ExitWorkflowIncomplete
from exit_workflow.services.contract_guard import assert_property_contractable

from .conftest import drive_to_damage_confirmed

pytestmark = pytest.mark.asyncio


# X-001 duplicate_initiation ------------------------------------------------


async def test_x001(client, tenancy, initiate_payload, engine):
    """Tenant initiates exit twice on the same contract -> 409 with existing id.
    Never a second workflow. (edges.yaml#X-001, rules.yaml#EXIT-01)"""
    row = await tenancy()
    payload = initiate_payload(row.contract_id)

    first = await client.post("/exit-workflows", json=payload, headers=row.tenant_headers)
    assert first.status_code == 201
    workflow_id = first.json()["workflow_id"]

    second = await client.post("/exit-workflows", json=payload, headers=row.tenant_headers)
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "EXIT_ALREADY_IN_PROGRESS"
    assert body["details"]["workflow_id"] == workflow_id

    async with engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM exit_workflows WHERE contract_id = :c"),
                {"c": row.contract_id},
            )
        ).scalar_one()
    assert count == 1


async def test_x001_concurrent_initiation_creates_one_workflow(
    client, tenancy, initiate_payload, engine
):
    """The partial unique index, not the pre-check, is what guarantees X-001."""
    row = await tenancy()
    payload = initiate_payload(row.contract_id)

    responses = await asyncio.gather(
        *(client.post("/exit-workflows", json=payload, headers=row.tenant_headers) for _ in range(4))
    )
    codes = sorted(response.status_code for response in responses)
    assert codes.count(201) == 1
    assert set(codes[1:]) == {409}

    async with engine.begin() as conn:
        count = (
            await conn.execute(
                text("SELECT count(*) FROM exit_workflows WHERE contract_id = :c"),
                {"c": row.contract_id},
            )
        ).scalar_one()
    assert count == 1


# X-002 notification_dispatch_fails ----------------------------------------


async def test_x002(client, tenancy, initiate_payload, engine, publisher, container, clock):
    """Owner notification fails after initiation commits: the workflow never
    rolls back, the event retries 5x with backoff, then dead-letters.
    (edges.yaml#X-002, rules.yaml#EXIT-04)"""
    row = await tenancy()
    publisher.fail_next = 99  # every attempt fails

    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    assert created.status_code == 201
    workflow_id = created.json()["workflow_id"]

    # The workflow survives the failed dispatch and holds at DOCS_SUBMITTED:
    # OWNER_NOTIFIED is only reached when the notification is actually emitted.
    async with engine.begin() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM exit_workflows WHERE id = :i"), {"i": workflow_id}
            )
        ).scalar_one()
        attempts, event_status = (
            await conn.execute(
                text("SELECT attempts, status FROM exit_workflow_events WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).one()
    assert status == "DOCS_SUBMITTED"
    assert (attempts, event_status) == (1, "PENDING")

    # Retries: 4 more attempts, each after its backoff has elapsed.
    for _ in range(4):
        clock.advance(hours=1)
        await container.notifications.dispatch_pending()

    async with engine.begin() as conn:
        attempts, event_status = (
            await conn.execute(
                text("SELECT attempts, status FROM exit_workflow_events WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).one()
        tasks = (
            await conn.execute(
                text(
                    "SELECT task_type FROM exit_workflow_admin_tasks WHERE workflow_id = :i"
                ),
                {"i": workflow_id},
            )
        ).scalars().all()
        still_there = (
            await conn.execute(
                text("SELECT count(*) FROM exit_workflows WHERE id = :i"), {"i": workflow_id}
            )
        ).scalar_one()

    assert attempts == 5  # rules.yaml#EXIT-04
    assert event_status == "DEAD_LETTERED"
    assert tasks == ["NOTIFICATION_DEAD_LETTER"]  # admin alert
    assert still_there == 1  # workflow NEVER rolls back


async def test_x002_recovers_when_the_broker_comes_back(
    client, tenancy, initiate_payload, engine, publisher, container, clock
):
    """A dispatch that succeeds on retry still moves DOCS_SUBMITTED -> OWNER_NOTIFIED."""
    row = await tenancy()
    publisher.fail_next = 1

    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]

    clock.advance(minutes=5)
    published = await container.notifications.dispatch_pending()
    assert published == 1

    async with engine.begin() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM exit_workflows WHERE id = :i"), {"i": workflow_id}
            )
        ).scalar_one()
    assert status == "OWNER_NOTIFIED"
    assert len(publisher.published) == 1


# X-003 damage_exceeds_deposit ---------------------------------------------


async def test_x003(client, tenancy, initiate_payload, engine):
    """confirmed_damage > security_deposit -> SpecUnresolved("R8"). No refund,
    no NOC, workflow holds at DAMAGE_CONFIRMED. (edges.yaml#X-003, risks.md#R8)"""
    row = await tenancy(deposit=Decimal("5000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("5000.01")
    )

    response = await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers
    )
    assert response.status_code == 501
    assert response.json()["code"] == "SPEC_UNRESOLVED_R8"

    async with engine.begin() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM exit_workflows WHERE id = :i"), {"i": workflow_id}
            )
        ).scalar_one()
        payments = (
            await conn.execute(
                text("SELECT count(*) FROM payments WHERE workflow_id = :i"), {"i": workflow_id}
            )
        ).scalar_one()
        nocs = (
            await conn.execute(
                text("SELECT count(*) FROM noc_documents WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).scalar_one()

    assert status == "DAMAGE_CONFIRMED"
    assert payments == 0
    assert nocs == 0


async def test_x003_damage_equal_to_deposit_is_not_blocked(
    client, tenancy, initiate_payload, engine
):
    """The R8 hold starts strictly above the deposit; equal settles to a zero
    refund (rules.yaml#EXIT-07: refund = max(deposit - damage, 0))."""
    row = await tenancy(deposit=Decimal("5000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("5000.00")
    )

    response = await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers
    )
    assert response.status_code == 200
    assert response.json()["refund_amount"] == "0.00"
    assert response.json()["status"] == "COMPLETE"


# X-004 gateway_pending_at_noc_time ----------------------------------------


async def test_x004(client, tenancy, initiate_payload, engine, gateway):
    """Refund still PENDING at NOC time -> refuse. NOC only after SUCCEEDED.
    (edges.yaml#X-004, rules.yaml#EXIT-08)"""
    row = await tenancy(deposit=Decimal("5000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("1000.00")
    )
    gateway.status = "PENDING"

    response = await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_PENDING"

    async with engine.begin() as conn:
        status, payment_status = (
            await conn.execute(
                text(
                    "SELECT w.status, p.status FROM exit_workflows w "
                    "JOIN payments p ON p.workflow_id = w.id WHERE w.id = :i"
                ),
                {"i": workflow_id},
            )
        ).one()
        nocs = (
            await conn.execute(
                text("SELECT count(*) FROM noc_documents WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).scalar_one()
        locked = (
            await conn.execute(
                text("SELECT exit_lock FROM properties WHERE id = :p"), {"p": row.property_id}
            )
        ).scalar_one()

    assert status == "DAMAGE_CONFIRMED"  # holds, never proceeds
    assert payment_status == "PENDING"
    assert nocs == 0
    assert locked is True  # released only by COMPLETE (rules.yaml#EXIT-03)


async def test_x004_failed_payment_also_holds(client, tenancy, initiate_payload, engine, gateway):
    """algorithm.md step 11 — PENDING or FAILED: hold, never proceed."""
    row = await tenancy(deposit=Decimal("5000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("1000.00")
    )
    gateway.status = "FAILED"

    response = await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers
    )
    assert response.status_code == 409
    # api.yaml defines no distinct code for a FAILED refund (blockers.md#B-005);
    # the payment row carries the true status.
    assert response.json()["code"] == "PAYMENT_PENDING"
    assert response.json()["details"]["payment_status"] == "FAILED"

    async with engine.begin() as conn:
        nocs = (
            await conn.execute(
                text("SELECT count(*) FROM noc_documents WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).scalar_one()
    assert nocs == 0


# X-005 concurrent_settlement ----------------------------------------------


async def test_x005(client, tenancy, initiate_payload, engine, gateway):
    """Two settlement attempts race: idempotency key = workflow_id means one
    payment, the second call returns the existing one. (edges.yaml#X-005)"""
    row = await tenancy(deposit=Decimal("5000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("1250.75")
    )

    first, second = await asyncio.gather(
        client.post(f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers),
        client.post(f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers),
    )

    assert {first.status_code, second.status_code} == {200}
    assert first.json()["payment_id"] == second.json()["payment_id"]
    assert first.json()["refund_amount"] == second.json()["refund_amount"] == "3749.25"

    async with engine.begin() as conn:
        payments = (
            await conn.execute(
                text("SELECT count(*) FROM payments WHERE workflow_id = :i"), {"i": workflow_id}
            )
        ).scalar_one()
        keys = (
            await conn.execute(
                text("SELECT idempotency_key FROM payments WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).scalars().all()
        nocs = (
            await conn.execute(
                text("SELECT count(*) FROM noc_documents WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).scalar_one()

    assert payments == 1
    assert keys == [workflow_id]  # rules.yaml#EXIT-08
    assert nocs == 1  # and exactly one NOC


# X-006 exit_lock_vs_new_contract ------------------------------------------


async def test_x006(client, tenancy, initiate_payload, engine, container):
    """A new contract attempted on the property mid-exit -> 409
    EXIT_WORKFLOW_INCOMPLETE. (edges.yaml#X-006, rules.yaml#EXIT-03, BR-1)"""
    row = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]

    async with container.session_factory() as session:
        with pytest.raises(ExitWorkflowIncomplete) as raised:
            await assert_property_contractable(session, row.property_id)

    assert raised.value.http_status == 409
    assert raised.value.code == "EXIT_WORKFLOW_INCOMPLETE"
    assert raised.value.details["workflow_id"] == workflow_id


async def test_x006_lock_released_only_by_complete(
    client, tenancy, initiate_payload, engine, container
):
    """rules.yaml#EXIT-03 — the lock is released by COMPLETE, and then the
    property can take a new contract again."""
    row = await tenancy(deposit=Decimal("2000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("500.00")
    )
    settled = await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers
    )
    assert settled.json()["status"] == "COMPLETE"

    async with container.session_factory() as session:
        await assert_property_contractable(session, row.property_id)  # no raise


# X-007 timezone_of_dates --------------------------------------------------


async def test_x007(client, tenancy, initiate_payload, engine, clock):
    """move_out_date is a calendar day in Asia/Dubai, stored as a date, and
    comparisons use the Dubai calendar. (edges.yaml#X-007, D-001)

    The frozen clock sits at 2026-03-01 20:00 UTC, which is already
    2026-03-02 in Dubai: a date that is 'today' in Dubai and 'tomorrow' in UTC
    must be accepted, and the UTC day must be rejected as past.
    """
    dubai_today = clock.today_dubai()
    utc_today = clock.now_utc().date()
    assert dubai_today == utc_today + timedelta(days=1)  # the case under test

    row = await tenancy()
    accepted = await client.post(
        "/exit-workflows",
        json=initiate_payload(row.contract_id, day=dubai_today),
        headers=row.tenant_headers,
    )
    assert accepted.status_code == 201

    other = await tenancy()
    rejected = await client.post(
        "/exit-workflows",
        json=initiate_payload(other.contract_id, day=utc_today),
        headers=other.tenant_headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "MOVE_OUT_DATE_IN_PAST"

    async with engine.begin() as conn:
        stored_type = (
            await conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_name = 'exit_workflows' AND column_name = 'move_out_date'"
                )
            )
        ).scalar_one()
    assert stored_type == "date"  # stored as date, not datetime
