"""End-to-end flow and the invariants algorithm.md / rules.yaml state directly."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from exit_workflow.errors import SpecUnresolved

from .conftest import drive_to_damage_confirmed

pytestmark = pytest.mark.asyncio


async def test_full_flow_initiation_to_complete(
    client, tenancy, initiate_payload, engine, publisher, gateway
):
    """algorithm.md steps 1-13, end to end."""
    row = await tenancy(deposit=Decimal("5000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("1234.56")
    )

    settled = await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers
    )
    assert settled.status_code == 200
    body = settled.json()
    # rules.yaml#EXIT-07 — 5000.00 - 1234.56, Decimal, 2 dp.
    assert body["refund_amount"] == "3765.44"
    assert body["currency"] == "AED"
    assert body["status"] == "COMPLETE"

    async with engine.begin() as conn:
        workflow = (
            await conn.execute(
                text(
                    "SELECT status, refund_amount_minor, noc_document_id, completed_at "
                    "FROM exit_workflows WHERE id = :i"
                ),
                {"i": workflow_id},
            )
        ).one()
        payment = (
            await conn.execute(
                text("SELECT type, amount_minor, status, idempotency_key FROM payments "
                     "WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).one()
        noc = (
            await conn.execute(
                text("SELECT bucket, object_key, region, size_bytes FROM noc_documents "
                     "WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).one()
        exit_lock = (
            await conn.execute(
                text("SELECT exit_lock FROM properties WHERE id = :p"), {"p": row.property_id}
            )
        ).scalar_one()
        audit = (
            await conn.execute(
                text(
                    "SELECT from_state, to_state, actor_role FROM exit_workflow_audit "
                    "WHERE workflow_id = :i ORDER BY id"
                ),
                {"i": workflow_id},
            )
        ).all()

    assert workflow.status == "COMPLETE"
    assert workflow.refund_amount_minor == 376544
    assert workflow.noc_document_id is not None
    assert workflow.completed_at is not None

    assert payment.type == "DEPOSIT_REFUND"  # rules.yaml#EXIT-08
    assert payment.amount_minor == 376544
    assert payment.status == "SUCCEEDED"
    assert payment.idempotency_key == workflow_id

    assert noc.region == "me-central-1"  # rules.yaml#EXIT-09 — UAE region bucket
    assert noc.size_bytes > 0

    assert exit_lock is False  # released by COMPLETE (rules.yaml#EXIT-03)

    # rules.yaml#EXIT-10 — every state change audited, in order.
    assert [(row_.from_state, row_.to_state) for row_ in audit] == [
        (None, "INITIATED"),
        ("INITIATED", "DOCS_SUBMITTED"),
        ("DOCS_SUBMITTED", "OWNER_NOTIFIED"),
        ("OWNER_NOTIFIED", "INSPECTION_SCHEDULED"),
        ("INSPECTION_SCHEDULED", "INSPECTION_DONE"),
        ("INSPECTION_DONE", "DAMAGE_CONFIRMED"),
        ("DAMAGE_CONFIRMED", "REFUND_PROCESSED"),
        ("REFUND_PROCESSED", "NOC_ISSUED"),
        ("NOC_ISSUED", "COMPLETE"),
    ]
    assert len(publisher.published) == 1  # rules.yaml#EXIT-04
    assert gateway.calls[0].idempotency_key == workflow_id


async def test_workflow_id_format(client, tenancy, initiate_payload, clock):
    """rules.yaml#EXIT-02 — EX-YYYYMMDD-NNNNN, date part in Asia/Dubai."""
    row = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]
    assert workflow_id.startswith(f"EX-{clock.today_dubai():%Y%m%d}-")
    assert len(workflow_id) == len("EX-YYYYMMDD-NNNNN")


async def test_initiation_rejects_non_active_contract(client, tenancy, initiate_payload):
    """algorithm.md step 1 / rules.yaml#EXIT-01 — 422 (no code, blockers.md#B-004)."""
    row = await tenancy(status="TERMINATED")
    response = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    assert response.status_code == 422
    assert response.json()["code"] is None


async def test_initiation_requires_documents(client, tenancy, initiate_payload):
    """rules.yaml#EXIT-02 — at least one document."""
    row = await tenancy()
    payload = initiate_payload(row.contract_id, documents=0)
    response = await client.post("/exit-workflows", json=payload, headers=row.tenant_headers)
    # Request-schema rejection; the domain rule is covered in tests/unit.
    assert response.status_code == 422


async def test_initiation_rejects_unknown_reason(client, tenancy, initiate_payload):
    """rules.yaml#EXIT-02 — reason must come from the reference list."""
    row = await tenancy()
    payload = initiate_payload(row.contract_id, reason="BECAUSE")
    response = await client.post("/exit-workflows", json=payload, headers=row.tenant_headers)
    assert response.status_code == 422
    assert response.json()["code"] == "REASON_INVALID"


async def test_settlement_before_owner_confirmation_is_refused(
    client, tenancy, initiate_payload, engine
):
    """states.yaml forbids INSPECTION_DONE -> REFUND_PROCESSED; owner
    confirmation is mandatory (rules.yaml#EXIT-06)."""
    row = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]
    await client.post(
        f"/exit-workflows/{workflow_id}/schedule-inspection",
        json={},
        headers=row.owner_headers,
    )
    await client.post(
        f"/exit-workflows/{workflow_id}/inspection-report",
        json={"damage_amount": "10.00", "photos": [{"photo_id": "p1"}]},
        headers=row.agency_headers,
    )

    response = await client.post(
        f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "WRONG_STATE"


async def test_out_of_order_step_is_wrong_state(client, tenancy, initiate_payload):
    """api.yaml 409 WRONG_STATE — confirm-damage before an inspection report."""
    row = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]

    response = await client.post(
        f"/exit-workflows/{workflow_id}/confirm-damage", headers=row.owner_headers
    )
    assert response.status_code == 409
    assert response.json()["code"] == "WRONG_STATE"


async def test_owner_of_another_property_cannot_act(client, tenancy, initiate_payload):
    """api.yaml authz: owner — of this workflow."""
    row = await tenancy()
    stranger = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]

    response = await client.post(
        f"/exit-workflows/{workflow_id}/schedule-inspection",
        json={},
        headers=stranger.owner_headers,
    )
    assert response.status_code == 403


async def test_tenant_cannot_initiate_on_someone_elses_contract(
    client, tenancy, initiate_payload
):
    """api.yaml authz: tenant, own active contract only."""
    row = await tenancy()
    stranger = await tenancy()
    response = await client.post(
        "/exit-workflows",
        json=initiate_payload(row.contract_id),
        headers=stranger.tenant_headers,
    )
    assert response.status_code == 403


async def test_stall_sweep_moves_overdue_workflow(
    client, tenancy, initiate_payload, engine, container, clock
):
    """rules.yaml#EXIT-05 — 30 days past move_out_date -> STALLED + admin task,
    and it does not auto-cancel."""
    row = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]

    clock.advance(days=30)
    assert await container.stall.sweep() == []  # exactly 30 days is still inside

    clock.advance(days=1)
    assert await container.stall.sweep() == [workflow_id]
    assert await container.stall.sweep() == []  # idempotent

    async with engine.begin() as conn:
        status, stalled_at = (
            await conn.execute(
                text("SELECT status, stalled_at FROM exit_workflows WHERE id = :i"),
                {"i": workflow_id},
            )
        ).one()
        tasks = (
            await conn.execute(
                text("SELECT task_type, status FROM exit_workflow_admin_tasks "
                     "WHERE workflow_id = :i"),
                {"i": workflow_id},
            )
        ).all()
        exit_lock = (
            await conn.execute(
                text("SELECT exit_lock FROM properties WHERE id = :p"), {"p": row.property_id}
            )
        ).scalar_one()

    assert status == "STALLED"
    assert stalled_at is not None
    assert tasks == [("STALLED_EXIT", "OPEN")]
    assert exit_lock is True  # not cancelled, lock still held


async def test_audit_rows_are_append_only(client, tenancy, initiate_payload, engine):
    """AGENTS.md / rules.yaml#EXIT-10 — enforced by DB trigger, not application code."""
    row = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]

    for statement in (
        "UPDATE exit_workflow_audit SET actor_id = 'tampered' WHERE workflow_id = :i",
        "DELETE FROM exit_workflow_audit WHERE workflow_id = :i",
    ):
        with pytest.raises(DBAPIError) as raised:
            async with engine.begin() as conn:
                await conn.execute(text(statement), {"i": workflow_id})
        assert "append-only" in str(raised.value)

    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE exit_workflow_audit"))


async def test_noc_row_is_immutable(client, tenancy, initiate_payload, engine):
    """rules.yaml#EXIT-09 — immutable once issued."""
    row = await tenancy(deposit=Decimal("1000.00"))
    workflow_id = await drive_to_damage_confirmed(
        client, row, initiate_payload(row.contract_id), damage=Decimal("0.00")
    )
    await client.post(f"/exit-workflows/{workflow_id}/settle", headers=row.owner_headers)

    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE noc_documents SET object_key = 'x' WHERE workflow_id = :i"),
                {"i": workflow_id},
            )


async def test_noc_object_is_write_once(object_store, tmp_path):
    """rules.yaml#EXIT-09 — the stored object cannot be replaced either."""
    from exit_workflow.adapters.object_store import ObjectAlreadyExists

    await object_store.put_immutable("k/noc.pdf", b"%PDF-1.4", "application/pdf")
    with pytest.raises(ObjectAlreadyExists):
        await object_store.put_immutable("k/noc.pdf", b"%PDF-1.4 other", "application/pdf")


async def test_owner_dispute_is_blocked(client, tenancy, initiate_payload, container):
    """rules.yaml#EXIT-06 gives the owner one dispute; states.yaml and api.yaml
    define no state, transition or endpoint for it (blockers.md#B-002)."""
    row = await tenancy()
    created = await client.post(
        "/exit-workflows", json=initiate_payload(row.contract_id), headers=row.tenant_headers
    )
    workflow_id = created.json()["workflow_id"]

    from exit_workflow.services.transitions import Principal
    from exit_workflow.domain.states import Actor

    with pytest.raises(SpecUnresolved) as raised:
        await container.inspection.dispute_damage(
            workflow_id, Principal(id=str(row.owner_id), role=Actor.OWNER)
        )
    assert raised.value.item == "B-002"
    assert raised.value.http_status == 501
