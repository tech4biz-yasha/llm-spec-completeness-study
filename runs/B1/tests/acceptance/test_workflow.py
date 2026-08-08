"""End-to-end coverage of algorithm.md steps 1 to 13 and the rules behind them."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from exit_workflow.domain.enums import PaymentStatus
from exit_workflow.domain.ids import is_workflow_id
from exit_workflow.domain.money import from_minor
from exit_workflow.domain.reasons import ConfiguredExitReasons
from exit_workflow.domain.states import State
from exit_workflow.services.noc import object_key
from tests import helpers
from tests.conftest import FIXTURE_REASONS


async def test_full_exit_journey(
    client, tenancy, initiate_body, session_factory, gateway, noc_storage, publisher
):
    """Initiation through COMPLETE, checking each rule's outcome on the way."""
    workflow_id = await helpers.initiate(client, tenancy, initiate_body)

    # rules.yaml#EXIT-02 — server-assigned ID, EX-YYYYMMDD-NNNNN.
    assert is_workflow_id(workflow_id)

    # rules.yaml#EXIT-04 — notification emitted after commit; the dispatcher
    # then advances DOCS_SUBMITTED -> OWNER_NOTIFIED.
    assert len(publisher.published) == 1
    assert publisher.published[0]["key"] == workflow_id
    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.OWNER_NOTIFIED

    # rules.yaml#EXIT-03 — exit lock held for the duration.
    assert await helpers.scalar(
        session_factory, "SELECT exit_lock FROM properties WHERE id = :id", id=tenancy.property_id
    )

    assert (await helpers.schedule_inspection(client, tenancy, workflow_id)).status_code == 200
    assert (
        await helpers.submit_report(client, tenancy, workflow_id, Decimal("1500.55"))
    ).status_code == 200
    assert (await helpers.confirm_damage(client, tenancy, workflow_id)).status_code == 200

    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.status_code == 200, response.text
    body = response.json()

    # rules.yaml#EXIT-07 — refund = deposit - damage, 2 dp, half-up.
    assert body["refund_amount"] == "10499.45"
    assert body["status"] == State.COMPLETE

    workflow = await helpers.workflow_row(session_factory, workflow_id)
    assert workflow["status"] == State.COMPLETE
    assert from_minor(workflow["refund_amount_minor"]) == Decimal("10499.45")
    assert workflow["completed_at"] is not None

    # rules.yaml#EXIT-08 — DEPOSIT_REFUND keyed on the workflow ID.
    payments = await helpers.rows(session_factory, "SELECT * FROM payments")
    assert len(payments) == 1
    assert payments[0]["payment_type"] == "DEPOSIT_REFUND"
    assert payments[0]["idempotency_key"] == workflow_id
    assert payments[0]["status"] == PaymentStatus.SUCCEEDED
    assert payments[0]["currency"] == "AED"

    # rules.yaml#EXIT-09 — PDF in the UAE bucket, linked to the workflow.
    documents = await helpers.rows(session_factory, "SELECT * FROM noc_documents")
    assert len(documents) == 1
    assert documents[0]["workflow_id"] == workflow_id
    assert documents[0]["region"] == "me-central-1"
    assert workflow["noc_document_id"] == documents[0]["id"]

    content = await noc_storage.get(object_key(workflow_id))
    assert content is not None
    assert content.startswith(b"%PDF-1.4")
    assert content.rstrip().endswith(b"%%EOF")

    # rules.yaml#EXIT-03, #EXIT-09 — lock released by COMPLETE, and only then.
    assert not await helpers.scalar(
        session_factory, "SELECT exit_lock FROM properties WHERE id = :id", id=tenancy.property_id
    )

    # rules.yaml#EXIT-10 — every state change audited, in order, with the actor.
    audit = await helpers.rows(
        session_factory,
        "SELECT * FROM exit_workflow_audit WHERE workflow_id = :id ORDER BY id",
        id=workflow_id,
    )
    assert [(row["from_state"], row["to_state"]) for row in audit] == [
        (None, State.INITIATED),
        (State.INITIATED, State.DOCS_SUBMITTED),
        (State.DOCS_SUBMITTED, State.OWNER_NOTIFIED),
        (State.OWNER_NOTIFIED, State.INSPECTION_SCHEDULED),
        (State.INSPECTION_SCHEDULED, State.INSPECTION_DONE),
        (State.INSPECTION_DONE, State.DAMAGE_CONFIRMED),
        (State.DAMAGE_CONFIRMED, State.REFUND_PROCESSED),
        (State.REFUND_PROCESSED, State.NOC_ISSUED),
        (State.NOC_ISSUED, State.COMPLETE),
    ]
    assert audit[1]["actor_type"] == "tenant"
    assert audit[1]["actor_id"] == str(tenancy.tenant_id)
    assert audit[3]["actor_type"] == "owner"


async def test_settle_is_idempotent_after_complete(client, tenancy, initiate_body, session_factory):
    """A repeated /settle on a COMPLETE workflow reports the stored outcome."""
    workflow_id = await helpers.drive_to_damage_confirmed(
        client, tenancy, initiate_body, damage=Decimal("100.00")
    )
    first = await helpers.settle(client, tenancy, workflow_id)
    second = await helpers.settle(client, tenancy, workflow_id)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert await helpers.scalar(session_factory, "SELECT count(*) FROM noc_documents") == 1


async def test_full_deposit_refund_when_no_damage(client, tenancy, initiate_body):
    """rules.yaml#EXIT-07 with zero damage refunds the whole deposit."""
    workflow_id = await helpers.drive_to_damage_confirmed(
        client, tenancy, initiate_body, damage=Decimal("0.00")
    )
    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.json()["refund_amount"] == "12000.00"


async def test_damage_equal_to_deposit_refunds_zero(client, tenancy, initiate_body):
    """rules.yaml#EXIT-07 boundary: damage == deposit is settled, not blocked.

    Only damage *greater* than the deposit is the open R8 case.
    """
    workflow_id = await helpers.drive_to_damage_confirmed(
        client, tenancy, initiate_body, damage=tenancy.deposit
    )
    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.status_code == 200
    assert response.json()["refund_amount"] == "0.00"
    assert response.json()["status"] == State.COMPLETE


# --- Order and state guards ---------------------------------------------------


async def test_settle_before_owner_confirmation_is_refused(client, tenancy, initiate_body):
    """states.yaml forbids INSPECTION_DONE -> REFUND_PROCESSED (rules.yaml#EXIT-06)."""
    workflow_id = await helpers.initiate(client, tenancy, initiate_body)
    await helpers.schedule_inspection(client, tenancy, workflow_id)
    await helpers.submit_report(client, tenancy, workflow_id, Decimal("10.00"))

    response = await helpers.settle(client, tenancy, workflow_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WRONG_STATE"
    assert response.json()["error"]["details"]["current_status"] == State.INSPECTION_DONE


async def test_confirm_damage_before_report_is_refused(client, tenancy, initiate_body):
    workflow_id = await helpers.initiate(client, tenancy, initiate_body)
    await helpers.schedule_inspection(client, tenancy, workflow_id)

    response = await helpers.confirm_damage(client, tenancy, workflow_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WRONG_STATE"


async def test_schedule_inspection_before_owner_notified_is_refused(
    app, client, tenancy, initiate_body, publisher
):
    """OWNER_NOTIFIED is the only source states.yaml gives for INSPECTION_SCHEDULED."""
    publisher.fail = True  # workflow stays DOCS_SUBMITTED
    workflow_id = await helpers.initiate(client, tenancy, initiate_body)

    response = await helpers.schedule_inspection(client, tenancy, workflow_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WRONG_STATE"


async def test_stalled_workflow_cannot_be_resumed_or_completed(
    client, tenancy, initiate_body, session_factory
):
    """states.yaml gives STALLED no outgoing edge at all (blockers.md#B-2)."""
    workflow_id = await helpers.initiate(client, tenancy, initiate_body)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE exit_workflows SET status = 'STALLED' WHERE id = :id"),
                {"id": workflow_id},
            )

    for call in (helpers.schedule_inspection, helpers.confirm_damage, helpers.settle):
        response = await call(client, tenancy, workflow_id)
        assert response.status_code == 409, call.__name__
        assert response.json()["error"]["code"] == "WRONG_STATE"


# --- Validation (rules.yaml#EXIT-01, #EXIT-02) --------------------------------


async def test_documents_required(client, tenancy, initiate_body):
    initiate_body["documents"] = []
    response = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "DOCUMENTS_REQUIRED"


async def test_reason_outside_reference_list(client, tenancy, initiate_body):
    initiate_body["reason"] = "NOT_IN_THE_LIST"
    response = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "REASON_INVALID"


async def test_move_out_date_in_past(client, tenancy, initiate_body, today_dubai):
    from datetime import timedelta

    initiate_body["move_out_date"] = (today_dubai - timedelta(days=1)).isoformat()
    response = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MOVE_OUT_DATE_IN_PAST"


async def test_move_out_date_today_is_accepted(client, tenancy, initiate_body, today_dubai):
    """rules.yaml#EXIT-02 — "today or later"."""
    initiate_body["move_out_date"] = today_dubai.isoformat()
    response = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    assert response.status_code == 201


async def test_inactive_contract_is_refused(client, tenancy, initiate_body, session_factory):
    """rules.yaml#EXIT-01 — ACTIVE only. api.yaml defines no code (blockers.md#B-4)."""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text("UPDATE contracts SET status = 'TERMINATED' WHERE id = :id"),
                {"id": tenancy.contract_id},
            )

    response = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] is None
    assert error["blocker"] == "B-4"


# --- Authorization (api.yaml authz lines) -------------------------------------


async def test_tenant_cannot_initiate_on_another_tenants_contract(
    client, tenancy, initiate_body
):
    import uuid

    response = await client.post(
        "/exit-workflows",
        json=initiate_body,
        headers=tenancy.headers("tenant", actor_id=uuid.uuid4()),
    )
    # Reported as not-found, so the endpoint cannot be used to enumerate contracts.
    assert response.status_code == 404


async def test_owner_cannot_initiate(client, tenancy, initiate_body):
    response = await client.post(
        "/exit-workflows", json=initiate_body, headers=tenancy.headers("owner")
    )
    assert response.status_code in (403, 404)


async def test_only_owner_may_confirm_damage(client, tenancy, initiate_body):
    workflow_id = await helpers.initiate(client, tenancy, initiate_body)
    await helpers.schedule_inspection(client, tenancy, workflow_id)
    await helpers.submit_report(client, tenancy, workflow_id, Decimal("10.00"))

    response = await client.post(
        f"/exit-workflows/{workflow_id}/confirm-damage",
        headers=tenancy.headers("inspection_agency"),
    )
    assert response.status_code == 403


async def test_unconfigured_principal_resolver_refuses(
    settings, session_factory, publisher, gateway, noc_storage, clock, initiate_body
):
    """The module fails closed when the platform binds no session layer."""
    from httpx import ASGITransport, AsyncClient

    from exit_workflow.app import create_app

    app = create_app(
        settings=settings,
        session_factory=session_factory,
        publisher=publisher,
        payment_gateway=gateway,
        noc_storage=noc_storage,
        exit_reasons=ConfiguredExitReasons(settings.exit_reason_codes),
        clock=clock,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://exit-workflow.test") as client:
        response = await client.post("/exit-workflows", json=initiate_body)
    assert response.status_code == 403


# --- Blocked branches ---------------------------------------------------------


async def test_initiation_blocked_when_reason_list_unpublished(
    settings, session_factory, publisher, gateway, noc_storage, clock, tenancy, initiate_body
):
    """blockers.md#B-1 — no reference list, no guess.

    risks.md lists the exit reason dictionary as an open item, so the module
    refuses to judge a reason rather than accepting or rejecting one on invented
    grounds.
    """
    from httpx import ASGITransport, AsyncClient

    from exit_workflow.api.security import HeaderPrincipalResolver
    from exit_workflow.app import create_app

    app = create_app(
        settings=settings,
        session_factory=session_factory,
        principal_resolver=HeaderPrincipalResolver(allow=True),
        publisher=publisher,
        payment_gateway=gateway,
        noc_storage=noc_storage,
        exit_reasons=ConfiguredExitReasons(None),  # not published
        clock=clock,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://exit-workflow.test") as client:
        response = await client.post(
            "/exit-workflows", json=initiate_body, headers=tenancy.headers("tenant")
        )

    assert response.status_code == 501
    error = response.json()["error"]
    assert error["blocker"] == "B-1"
    # api.yaml defines a SPEC_UNRESOLVED code for R8 only; none is invented here.
    assert error["code"] is None

    assert await helpers.scalar(session_factory, "SELECT count(*) FROM exit_workflows") == 0


async def test_tenant_scoped_contract_guard_is_blocked(session_factory, tenancy):
    """risks.md#R1 — role-scope versus identity-scope for BR-1 is undecided."""
    from exit_workflow.domain.errors import SpecUnresolved
    from exit_workflow.services.guards import assert_tenant_contractable

    async with session_factory() as session:
        with pytest.raises(SpecUnresolved) as raised:
            await assert_tenant_contractable(session, tenancy.tenant_id)
    assert raised.value.blocker_id == "R1"


async def test_no_dispute_endpoint_exists(app):
    """rules.yaml#EXIT-06 grants a dispute; states.yaml and api.yaml define none.

    blockers.md#B-5 — nothing is built for it rather than a path being invented.
    """
    paths = set(app.openapi()["paths"])
    assert not any("dispute" in path for path in paths)
    # The published surface is exactly the five paths api.yaml defines.
    assert paths == {
        "/exit-workflows",
        "/exit-workflows/{workflow_id}/schedule-inspection",
        "/exit-workflows/{workflow_id}/inspection-report",
        "/exit-workflows/{workflow_id}/confirm-damage",
        "/exit-workflows/{workflow_id}/settle",
    }


async def test_reference_list_is_not_shipped():
    """The module must not carry an invented exit reason vocabulary."""
    from exit_workflow.config import Settings

    assert Settings().exit_reason_codes is None
    assert ConfiguredExitReasons(None).codes() is None
    assert set(FIXTURE_REASONS).isdisjoint(ConfiguredExitReasons([]).codes() or set())
