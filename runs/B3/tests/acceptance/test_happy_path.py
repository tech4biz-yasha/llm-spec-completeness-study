"""algorithm.md end to end: initiation through COMPLETE."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select

from exit_workflow.db.models import NocDocument, Payment, Property
from exit_workflow.enums import PaymentStatus, WorkflowState

from ..conftest import CONTRACT_ID, DEPOSIT, PROPERTY_ID
from ..support import audit_trail, load, status


def test_full_workflow_over_http(
    client,
    session_factory,
    storage,
    publisher,
    move_out_date,
    tenant_headers,
    owner_headers,
    agency_headers,
    system_headers,
):
    created = client.post(
        "/exit-workflows",
        json={
            "contract_id": CONTRACT_ID,
            "move_out_date": move_out_date.isoformat(),
            "reason": "END_OF_TENANCY",
            "documents": [{"type": "EJARI", "id": "DOC-1"}],
        },
        headers=tenant_headers,
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow_id"]

    # rules.yaml#EXIT-02 — EX-YYYYMMDD-NNNNN, the date being today in Asia/Dubai.
    assert workflow_id == "EX-20260301-00001"

    # algorithm.md#4 — the initiation transaction ends at DOCS_SUBMITTED.
    assert created.json()["status"] == WorkflowState.DOCS_SUBMITTED

    # rules.yaml#EXIT-03 — the property is locked by the same transaction.
    with session_factory() as session:
        prop = session.get(Property, PROPERTY_ID)
        assert prop.exit_lock is True
        assert prop.exit_lock_workflow_id == workflow_id

    # rules.yaml#EXIT-04 — notification emitted after commit (TestClient runs the
    # background task before returning control).
    assert [event.key for event in publisher.published] == [workflow_id]
    assert status(session_factory, workflow_id) is WorkflowState.OWNER_NOTIFIED

    assert (
        client.post(
            f"/exit-workflows/{workflow_id}/schedule-inspection", headers=owner_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/exit-workflows/{workflow_id}/inspection-report",
            json={"damage_amount": "1500.50", "photos": ["s3://photos/1.jpg"]},
            headers=agency_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/exit-workflows/{workflow_id}/confirm-damage", headers=owner_headers
        ).status_code
        == 200
    )

    settled = client.post(f"/exit-workflows/{workflow_id}/settle", headers=system_headers)
    assert settled.status_code == 200, settled.text
    body = settled.json()

    # rules.yaml#EXIT-07 — refund = deposit - damage, Decimal, half-up 2 dp.
    assert Decimal(body["refund_amount"]) == DEPOSIT - Decimal("1500.50")
    assert body["status"] == WorkflowState.COMPLETE

    with session_factory() as session:
        payment = session.execute(select(Payment)).scalar_one()
        # rules.yaml#EXIT-08
        assert payment.type == "DEPOSIT_REFUND"
        assert payment.idempotency_key == workflow_id
        assert payment.status == PaymentStatus.SUCCEEDED
        assert payment.amount_minor == 849_950

        noc = session.execute(select(NocDocument)).scalar_one()
        # rules.yaml#EXIT-09 — PDF in the UAE bucket, linked on the workflow.
        assert noc.region == "me-central-1"
        assert noc.content_type == "application/pdf"
        assert storage.objects[(noc.bucket, noc.object_key)].startswith(b"%PDF-1.4")

        # rules.yaml#EXIT-09 — exit lock released by COMPLETE.
        prop = session.get(Property, PROPERTY_ID)
        assert prop.exit_lock is False
        assert prop.exit_lock_workflow_id is None

    workflow = load(session_factory, workflow_id)
    assert workflow.state is WorkflowState.COMPLETE
    assert workflow.noc_document_id is not None
    assert workflow.refund_amount_minor == 849_950

    # rules.yaml#EXIT-10 — every state change wrote an audit row, in T13 order.
    trail = audit_trail(session_factory, workflow_id)
    assert [row.to_state for row in trail] == [
        WorkflowState.INITIATED,
        WorkflowState.DOCS_SUBMITTED,
        WorkflowState.OWNER_NOTIFIED,
        WorkflowState.INSPECTION_SCHEDULED,
        WorkflowState.INSPECTION_DONE,
        WorkflowState.DAMAGE_CONFIRMED,
        WorkflowState.REFUND_PROCESSED,
        WorkflowState.NOC_ISSUED,
        WorkflowState.COMPLETE,
    ]
    assert all(row.actor_role for row in trail)
    assert trail[1].rule_id == "EXIT-02"
    assert trail[-1].rule_id == "EXIT-09"


def test_refund_is_exact_when_damage_is_zero(
    service, session_factory, tenant, owner, agency, system, move_out_date
):
    from ..support import drive_to_damage_confirmed

    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("0.00"),
    )
    result = service.settle(workflow_id, principal=system)
    # rules.yaml#EXIT-07
    assert result.refund_amount == DEPOSIT
    assert result.status is WorkflowState.COMPLETE


def test_refund_of_exactly_the_deposit_leaves_zero(
    service, session_factory, tenant, owner, agency, system, move_out_date
):
    """rules.yaml#EXIT-07 boundary: damage == deposit is settled, not blocked."""
    from ..support import drive_to_damage_confirmed

    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=DEPOSIT,
    )
    result = service.settle(workflow_id, principal=system)
    assert result.refund_amount == Decimal("0.00")
    assert result.status is WorkflowState.COMPLETE
