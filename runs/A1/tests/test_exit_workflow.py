"""End-to-end coverage of the exit workflow (SRS T13, O15, O16, BR-1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.domain.states import ExitWorkflowState
from app.models import AuditLogEntry, ExitWorkflow, OutboxEvent
from tests.conftest import DEPOSIT_FILS, Actors, future_slot, move_out_date, seed_actors

API = "/api/v1"


# --- helpers ---------------------------------------------------------------------------


async def initiate(client, actors: Actors, *, days: int = 30) -> dict:
    response = await client.post(
        f"{API}/exit-workflows",
        json={
            "contract_id": str(actors.contract.id),
            "move_out_date": move_out_date(days),
            "reason_code": "LEASE_EXPIRY",
        },
        headers=actors.tenant_auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def upload_required_document(client, actors: Actors, workflow_id: str) -> None:
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/documents",
        json={
            "kind": "EMIRATES_ID",
            "file_name": "emirates-id.pdf",
            "content_type": "application/pdf",
            "byte_size": 88_512,
            "storage_key": f"exits/{workflow_id}/emirates-id.pdf",
        },
        headers=actors.tenant_auth,
    )
    assert response.status_code == 201, response.text


async def advance_to_damage_review(
    client, actors: Actors, *, deduction_fils: int = 125_000
) -> str:
    """Drive a fresh workflow up to the damage-review step and return its id."""
    workflow = await initiate(client, actors)
    workflow_id = workflow["id"]

    await upload_required_document(client, actors, workflow_id)
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/submit", headers=actors.tenant_auth
    )
    assert response.status_code == 200, response.text

    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/approve",
        json={"agency_id": str(actors.agency.id)},
        headers=actors.owner_auth,
    )
    assert response.status_code == 200, response.text

    assignments = await client.get(f"{API}/agency/assignments", headers=actors.agency_auth)
    assignment_id = assignments.json()["items"][0]["id"]

    starts_at, ends_at = future_slot()
    response = await client.post(
        f"{API}/agency/assignments/{assignment_id}/slots",
        json={"slots": [{"starts_at": starts_at, "ends_at": ends_at}]},
        headers=actors.agency_auth,
    )
    assert response.status_code == 200, response.text
    slot_id = response.json()["slots"][0]["id"]

    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/inspection/select-slot",
        json={"slot_id": slot_id},
        headers=actors.tenant_auth,
    )
    assert response.status_code == 200, response.text

    line_items = (
        [
            {
                "code": "PAINT",
                "description": "Repainting required in the master bedroom",
                "severity": "MODERATE",
                "amount_fils": deduction_fils,
                "location": "Master bedroom",
                "photos": [{"storage_key": "photos/paint-1.jpg", "caption": "Wall damage"}],
            }
        ]
        if deduction_fils > 0
        else []
    )
    response = await client.post(
        f"{API}/agency/assignments/{assignment_id}/report",
        json={
            "summary": "Property inspected. Minor damage recorded.",
            "inspected_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "inspector_name": "Rashid Ahmed",
            "line_items": line_items,
        },
        headers=actors.agency_auth,
    )
    assert response.status_code == 201, response.text
    return workflow_id


# --- tests ------------------------------------------------------------------------------


async def test_health(client) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"


async def test_full_exit_workflow_end_to_end(client, actors: Actors, session) -> None:
    """T13's ten steps, from the exit request through to NOC download and completion."""
    # Steps 1-3 and 5: move-out date, reason, workflow ID generation.
    workflow = await initiate(client, actors)
    workflow_id = workflow["id"]
    assert workflow["reference"].startswith("EXW-")
    assert workflow["state"] == ExitWorkflowState.DOCUMENTS_PENDING
    assert workflow["missing_required_documents"] == ["EMIRATES_ID"]
    assert workflow["deposit"]["fils"] == DEPOSIT_FILS
    assert workflow["progress"]["current_step"] == 4

    # Step 4: document upload.
    await upload_required_document(client, actors, workflow_id)

    # Step 6: submission and owner notification.
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/submit", headers=actors.tenant_auth
    )
    assert response.status_code == 200
    assert response.json()["state"] == ExitWorkflowState.PENDING_OWNER_APPROVAL
    assert response.json()["missing_required_documents"] == []

    # Owner approves and names the inspection agency (Appendix B).
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/approve",
        json={"agency_id": str(actors.agency.id), "instructions": "Keys with the concierge"},
        headers=actors.owner_auth,
    )
    assert response.status_code == 200
    assert response.json()["state"] == ExitWorkflowState.INSPECTION_SCHEDULING

    # Step 7: the agency proposes dates and a party selects one.
    assignments = await client.get(f"{API}/agency/assignments", headers=actors.agency_auth)
    assert assignments.status_code == 200
    assignment = assignments.json()["items"][0]
    assert assignment["status"] == "REQUESTED"
    assert assignment["instructions"] == "Keys with the concierge"

    starts_at, ends_at = future_slot()
    starts_at_2, ends_at_2 = future_slot(days=5)
    response = await client.post(
        f"{API}/agency/assignments/{assignment['id']}/slots",
        json={
            "slots": [
                {"starts_at": starts_at, "ends_at": ends_at},
                {"starts_at": starts_at_2, "ends_at": ends_at_2},
            ]
        },
        headers=actors.agency_auth,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "SLOTS_PROPOSED"
    assert len(response.json()["slots"]) == 2
    slot_id = response.json()["slots"][0]["id"]

    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/inspection/select-slot",
        json={"slot_id": slot_id},
        headers=actors.tenant_auth,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "SCHEDULED"

    detail = await client.get(f"{API}/exit-workflows/{workflow_id}", headers=actors.tenant_auth)
    assert detail.json()["state"] == ExitWorkflowState.INSPECTION_SCHEDULED

    # Step 8: the agency uploads the damage report with photos.
    response = await client.post(
        f"{API}/agency/assignments/{assignment['id']}/report",
        json={
            "summary": "Property inspected. Repainting required in the master bedroom.",
            "inspected_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
            "inspector_name": "Rashid Ahmed",
            "line_items": [
                {
                    "code": "PAINT",
                    "description": "Repainting required in the master bedroom",
                    "severity": "MODERATE",
                    "amount_fils": 100_000,
                    "location": "Master bedroom",
                    "photos": [{"storage_key": "photos/paint-1.jpg"}],
                },
                {
                    "code": "AC_SERVICE",
                    "description": "Air-conditioning service overdue",
                    "severity": "MINOR",
                    "amount_fils": 25_000,
                },
            ],
        },
        headers=actors.agency_auth,
    )
    assert response.status_code == 201, response.text
    assert response.json()["total_deductions"]["fils"] == 125_000

    detail = await client.get(f"{API}/exit-workflows/{workflow_id}", headers=actors.tenant_auth)
    assert detail.json()["state"] == ExitWorkflowState.DAMAGE_REVIEW
    assert detail.json()["progress"]["current_step"] == 8

    # Step 9: deposit minus damage.
    settlement = await client.get(
        f"{API}/exit-workflows/{workflow_id}/settlement", headers=actors.tenant_auth
    )
    assert settlement.status_code == 200
    body = settlement.json()
    assert body["status"] == "DRAFT"
    assert body["deposit"]["fils"] == DEPOSIT_FILS
    assert body["total_deductions"]["fils"] == 125_000
    assert body["refund"]["fils"] == 375_000
    assert body["refund"]["amount"] == "3750.00"
    assert body["balance_due"]["fils"] == 0
    assert body["tenant_owes_balance"] is False

    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
    )
    assert response.status_code == 200
    assert response.json()["status"] == "PAYABLE"

    # The owner clicks "Pay Deposit"; the NOC is generated automatically on payment.
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND"},
        headers={**actors.owner_auth, "Idempotency-Key": f"pay-{workflow_id}"},
    )
    assert response.status_code == 200, response.text
    paid = response.json()
    assert paid["status"] == "CLOSED"
    assert paid["payments"][0]["status"] == "SUCCEEDED"
    assert paid["payments"][0]["amount"]["fils"] == 375_000

    detail = await client.get(f"{API}/exit-workflows/{workflow_id}", headers=actors.tenant_auth)
    assert detail.json()["state"] == ExitWorkflowState.NOC_ISSUED

    # Step 10: digital NOC download, then completion.
    meta = await client.get(f"{API}/exit-workflows/{workflow_id}/noc", headers=actors.tenant_auth)
    assert meta.status_code == 200
    assert meta.json()["noc_number"].startswith("NOC-")
    assert meta.json()["download_count"] == 0

    download = await client.get(
        f"{API}/exit-workflows/{workflow_id}/noc/download", headers=actors.tenant_auth
    )
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.content.startswith(b"%PDF-1.4")
    assert len(download.content) > 1000
    assert meta.json()["content_sha256"] == download.headers["x-content-sha256"]

    detail = await client.get(f"{API}/exit-workflows/{workflow_id}", headers=actors.tenant_auth)
    assert detail.json()["state"] == ExitWorkflowState.COMPLETED
    assert detail.json()["is_active"] is False
    assert detail.json()["completed_at"] is not None
    assert detail.json()["progress"]["current_step"] == 10

    # The full ten-step history is on the record, and the audit trail matches it.
    states = [t["to_state"] for t in detail.json()["transitions"]]
    assert states == [
        ExitWorkflowState.DOCUMENTS_PENDING,
        ExitWorkflowState.PENDING_OWNER_APPROVAL,
        ExitWorkflowState.OWNER_APPROVED,
        ExitWorkflowState.INSPECTION_SCHEDULING,
        ExitWorkflowState.INSPECTION_SCHEDULED,
        ExitWorkflowState.INSPECTION_COMPLETED,
        ExitWorkflowState.DAMAGE_REVIEW,
        ExitWorkflowState.PENDING_SETTLEMENT,
        ExitWorkflowState.SETTLED,
        ExitWorkflowState.NOC_ISSUED,
        ExitWorkflowState.COMPLETED,
    ]

    actions = (
        await session.execute(
            sa.select(AuditLogEntry.action)
            .where(AuditLogEntry.workflow_id == uuid.UUID(workflow_id))
            .order_by(AuditLogEntry.id)
        )
    ).scalars().all()
    for expected in (
        "WORKFLOW_INITIATED",
        "DOCUMENT_UPLOADED",
        "WORKFLOW_SUBMITTED",
        "OWNER_APPROVED",
        "INSPECTION_REQUESTED",
        "DAMAGE_REPORT_SUBMITTED",
        "SETTLEMENT_APPROVED",
        "PAYMENT_SUCCEEDED",
        "NOC_ISSUED",
        "NOC_DOWNLOADED",
        "WORKFLOW_COMPLETED",
    ):
        assert expected in actions, f"missing audit action {expected}"

    # Every side effect was staged transactionally rather than fired mid-transaction.
    pending = await session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent))
    assert pending > 0


async def test_invalid_state_transition(client, actors: Actors) -> None:
    """An operation attempted from the wrong state is refused with 409 and the legal set."""
    workflow = await initiate(client, actors)
    workflow_id = workflow["id"]

    # Approving before the tenant has submitted is not a legal move.
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/approve", json={}, headers=actors.owner_auth
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_state_transition"
    assert error["details"]["current_state"] == ExitWorkflowState.DOCUMENTS_PENDING
    assert error["details"]["attempted_state"] == ExitWorkflowState.OWNER_APPROVED
    assert ExitWorkflowState.PENDING_OWNER_APPROVAL in error["details"]["allowed_next_states"]

    # Paying before a settlement exists is refused too.
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND", "idempotency_key": "premature-payment"},
        headers=actors.owner_auth,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"

    # Submitting without the required document is a validation failure, not a transition.
    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/submit", headers=actors.tenant_auth
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["missing_document_kinds"] == ["EMIRATES_ID"]


async def test_duplicate_active_workflow_rejected(client, actors: Actors) -> None:
    """One in-flight exit per contract; a second attempt is a 409 naming the first."""
    first = await initiate(client, actors)

    response = await client.post(
        f"{API}/exit-workflows",
        json={
            "contract_id": str(actors.contract.id),
            "move_out_date": move_out_date(45),
            "reason_code": "RELOCATION",
        },
        headers=actors.tenant_auth,
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "workflow_already_active"
    assert error["details"]["existing_reference"] == first["reference"]

    # Cancelling the first releases the constraint.
    cancelled = await client.post(
        f"{API}/exit-workflows/{first['id']}/cancel",
        json={"reason": "Tenant decided to renew instead"},
        headers=actors.tenant_auth,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == ExitWorkflowState.CANCELLED

    retry = await client.post(
        f"{API}/exit-workflows",
        json={
            "contract_id": str(actors.contract.id),
            "move_out_date": move_out_date(45),
            "reason_code": "RELOCATION",
        },
        headers=actors.tenant_auth,
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["reference"] != first["reference"]


async def test_contract_lock_blocks_during_active_workflow(client, actors: Actors, session) -> None:
    """BR-1: no new contract for the property or the tenant while an exit is in flight."""
    workflow = await initiate(client, actors)

    eligibility = await client.get(
        f"{API}/contracts/eligibility",
        params={"property_id": str(actors.property.id), "tenant_id": str(actors.tenant.id)},
        headers=actors.owner_auth,
    )
    assert eligibility.status_code == 200
    body = eligibility.json()
    assert body["allowed"] is False
    scopes = {b["scope"] for b in body["blockers"]}
    assert scopes == {"PROPERTY", "TENANT"}
    assert all(workflow["reference"] in message for message in body["warnings"])
    assert len(body["warnings"]) == 2

    response = await client.post(
        f"{API}/contracts",
        json={
            "contract_number": "CTR-NEW-001",
            "property_id": str(actors.property.id),
            "tenant_id": str(actors.tenant.id),
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
            "security_deposit_fils": 500_000,
            "annual_rent_fils": 12_000_000,
        },
        headers=actors.owner_auth,
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "contract_blocked_by_exit_workflow"
    assert workflow["reference"] in error["message"]
    assert len(error["details"]["blockers"]) == 2

    # The blocked attempt is itself auditable.
    blocked = await session.scalar(
        sa.select(sa.func.count())
        .select_from(AuditLogEntry)
        .where(AuditLogEntry.action == "CONTRACT_BLOCKED")
    )
    assert blocked == 1

    # A different property belonging to the same owner is unaffected.
    other = await seed_actors(session, suffix="other")
    ok = await client.get(
        f"{API}/contracts/eligibility",
        params={"property_id": str(other.property.id)},
        headers=actors.owner_auth,
    )
    assert ok.json()["allowed"] is True


async def test_contract_lock_released_after_completion(client, actors: Actors) -> None:
    """BR-1: once the workflow completes, the property and tenant are free again."""
    workflow_id = await advance_to_damage_review(client, actors)

    await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
    )
    await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND", "idempotency_key": f"release-{workflow_id}"},
        headers=actors.owner_auth,
    )

    # Still locked while the NOC is issued but the workflow is not yet closed.
    blocked = await client.get(
        f"{API}/contracts/eligibility",
        params={"property_id": str(actors.property.id)},
        headers=actors.owner_auth,
    )
    assert blocked.json()["allowed"] is False

    download = await client.get(
        f"{API}/exit-workflows/{workflow_id}/noc/download", headers=actors.tenant_auth
    )
    assert download.status_code == 200

    eligibility = await client.get(
        f"{API}/contracts/eligibility",
        params={"property_id": str(actors.property.id), "tenant_id": str(actors.tenant.id)},
        headers=actors.owner_auth,
    )
    assert eligibility.json() == {"allowed": True, "blockers": [], "warnings": []}

    response = await client.post(
        f"{API}/contracts",
        json={
            "contract_number": "CTR-RENEWED-001",
            "property_id": str(actors.property.id),
            "tenant_id": str(actors.tenant.id),
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
            "security_deposit_fils": 550_000,
            "annual_rent_fils": 13_000_000,
        },
        headers=actors.owner_auth,
    )
    assert response.status_code == 201, response.text
    assert response.json()["contract_number"] == "CTR-RENEWED-001"


# --- settlement behaviour ---------------------------------------------------------------


async def test_damages_exceeding_deposit_create_a_tenant_balance(client, session) -> None:
    """Refund floors at zero; the excess becomes a balance the tenant must clear."""
    actors = await seed_actors(session, suffix="short", deposit_fils=500_000)
    workflow_id = await advance_to_damage_review(client, actors, deduction_fils=620_000)

    settlement = await client.get(
        f"{API}/exit-workflows/{workflow_id}/settlement", headers=actors.tenant_auth
    )
    body = settlement.json()
    assert body["refund"]["fils"] == 0
    assert body["balance_due"]["fils"] == 120_000
    assert body["tenant_owes_balance"] is True

    approved = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
    )
    # The zero-valued refund leg is satisfied on approval, but the settlement stays open.
    assert approved.json()["status"] == "PAYABLE"
    assert approved.json()["refund_settled_at"] is not None
    assert approved.json()["balance_settled_at"] is None

    # No NOC until the tenant clears the balance.
    noc = await client.get(f"{API}/exit-workflows/{workflow_id}/noc", headers=actors.tenant_auth)
    assert noc.status_code == 404

    # The owner cannot pay the tenant's leg.
    wrong_payer = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "TENANT_BALANCE", "idempotency_key": "owner-pays-tenant-leg"},
        headers=actors.owner_auth,
    )
    assert wrong_payer.status_code == 403

    paid = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "TENANT_BALANCE", "idempotency_key": f"balance-{workflow_id}"},
        headers=actors.tenant_auth,
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "CLOSED"

    noc = await client.get(f"{API}/exit-workflows/{workflow_id}/noc", headers=actors.tenant_auth)
    assert noc.status_code == 200
    assert noc.json()["snapshot"]["settlement"]["balance_due_fils"] == 120_000


async def test_zero_deduction_settlement_closes_on_approval(client, session) -> None:
    """A spotless inspection returns the whole deposit and issues the NOC immediately."""
    actors = await seed_actors(session, suffix="clean")
    workflow_id = await advance_to_damage_review(client, actors, deduction_fils=0)

    settlement = await client.get(
        f"{API}/exit-workflows/{workflow_id}/settlement", headers=actors.tenant_auth
    )
    assert settlement.json()["refund"]["fils"] == DEPOSIT_FILS

    approved = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
    )
    assert approved.json()["status"] == "PAYABLE"

    paid = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND", "idempotency_key": f"full-{workflow_id}"},
        headers=actors.owner_auth,
    )
    assert paid.json()["refund"]["fils"] == DEPOSIT_FILS
    assert paid.json()["status"] == "CLOSED"


async def test_payment_is_idempotent(client, actors: Actors, session) -> None:
    """Replaying "Pay Deposit" with the same key must not move money twice."""
    workflow_id = await advance_to_damage_review(client, actors)
    await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
    )

    key = f"idem-{workflow_id}"
    first = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND", "idempotency_key": key},
        headers=actors.owner_auth,
    )
    assert first.status_code == 200
    assert len(first.json()["payments"]) == 1

    replay = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND", "idempotency_key": key},
        headers=actors.owner_auth,
    )
    assert replay.status_code == 200, replay.text
    assert len(replay.json()["payments"]) == 1
    assert replay.json()["payments"][0]["id"] == first.json()["payments"][0]["id"]

    # A fresh key against an already-settled leg is refused rather than double-paying.
    duplicate = await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND", "idempotency_key": f"{key}-again"},
        headers=actors.owner_auth,
    )
    assert duplicate.status_code == 409

    total = await session.scalar(
        sa.text("SELECT COUNT(*) FROM payment_transactions WHERE workflow_id = :w"),
        {"w": uuid.UUID(workflow_id)},
    )
    assert total == 1


async def test_settlement_dispute_and_reinspection(client, actors: Actors) -> None:
    """A disputed report can be sent back for a second inspection."""
    workflow_id = await advance_to_damage_review(client, actors)

    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/inspection/reinspect",
        json={"agency_id": str(actors.agency.id), "reason": "Tenant contests the paint charge"},
        headers=actors.owner_auth,
    )
    assert response.status_code == 201, response.text
    assert response.json()["attempt"] == 2

    detail = await client.get(f"{API}/exit-workflows/{workflow_id}", headers=actors.tenant_auth)
    assert detail.json()["state"] == ExitWorkflowState.INSPECTION_SCHEDULING

    settlement = await client.get(
        f"{API}/exit-workflows/{workflow_id}/settlement", headers=actors.tenant_auth
    )
    assert settlement.json()["status"] == "VOID"
    assert settlement.json()["void_reason"] == "Tenant contests the paint charge"


# --- authorisation ------------------------------------------------------------------------


async def test_workflow_is_invisible_to_unrelated_parties(client, actors: Actors, session) -> None:
    workflow = await initiate(client, actors)
    stranger = await seed_actors(session, suffix="stranger")

    forbidden = await client.get(
        f"{API}/exit-workflows/{workflow['id']}", headers=stranger.tenant_auth
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    unauthenticated = await client.get(f"{API}/exit-workflows/{workflow['id']}")
    assert unauthenticated.status_code == 401

    bad_token = await client.get(
        f"{API}/exit-workflows/{workflow['id']}",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert bad_token.status_code == 401


async def test_agency_cannot_touch_another_agencys_assignment(
    client, actors: Actors, session
) -> None:
    workflow_id = await advance_to_damage_review(client, actors)
    other = await seed_actors(session, suffix="rival")

    assignments = await client.get(f"{API}/agency/assignments", headers=actors.agency_auth)
    assignment_id = assignments.json()["items"][0]["id"]

    starts_at, ends_at = future_slot()
    response = await client.post(
        f"{API}/agency/assignments/{assignment_id}/slots",
        json={"slots": [{"starts_at": starts_at, "ends_at": ends_at}]},
        headers=other.agency_auth,
    )
    assert response.status_code == 403

    # And a rival agency's assignment list stays empty.
    listing = await client.get(f"{API}/agency/assignments", headers=other.agency_auth)
    assert listing.json()["count"] == 0

    bad_key = await client.get(f"{API}/agency/assignments", headers={"X-Agency-Key": "nwa_bogus"})
    assert bad_key.status_code == 401


async def test_tenant_cannot_approve_own_exit(client, actors: Actors) -> None:
    workflow = await initiate(client, actors)
    await upload_required_document(client, actors, workflow["id"])
    await client.post(f"{API}/exit-workflows/{workflow['id']}/submit", headers=actors.tenant_auth)

    response = await client.post(
        f"{API}/exit-workflows/{workflow['id']}/approve", json={}, headers=actors.tenant_auth
    )
    assert response.status_code == 403


# --- validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "expected"),
    [(-1, 422), (400, 422), (30, 201)],
)
async def test_move_out_date_bounds(client, actors: Actors, days: int, expected: int) -> None:
    response = await client.post(
        f"{API}/exit-workflows",
        json={
            "contract_id": str(actors.contract.id),
            "move_out_date": move_out_date(days),
            "reason_code": "LEASE_EXPIRY",
        },
        headers=actors.tenant_auth,
    )
    assert response.status_code == expected, response.text


async def test_other_reason_requires_free_text(client, actors: Actors) -> None:
    response = await client.post(
        f"{API}/exit-workflows",
        json={
            "contract_id": str(actors.contract.id),
            "move_out_date": move_out_date(),
            "reason_code": "OTHER",
        },
        headers=actors.tenant_auth,
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["field"] == "reason_text"


async def test_cancellation_forbidden_after_settlement(client, actors: Actors) -> None:
    workflow_id = await advance_to_damage_review(client, actors)
    await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/approve", headers=actors.owner_auth
    )
    await client.post(
        f"{API}/exit-workflows/{workflow_id}/settlement/pay",
        json={"leg": "OWNER_REFUND", "idempotency_key": f"nocancel-{workflow_id}"},
        headers=actors.owner_auth,
    )

    response = await client.post(
        f"{API}/exit-workflows/{workflow_id}/cancel",
        json={"reason": "changed my mind"},
        headers=actors.tenant_auth,
    )
    assert response.status_code == 409
    assert "no longer be cancelled" in response.json()["error"]["message"]


async def test_workflow_state_is_consistent_with_is_active_flag(
    client, actors: Actors, session
) -> None:
    """The DB check constraint keeps the BR-1 lock flag honest."""
    workflow = await initiate(client, actors)
    row = await session.get(ExitWorkflow, uuid.UUID(workflow["id"]))
    assert row is not None and row.is_active is True

    with pytest.raises(sa.exc.IntegrityError):
        await session.execute(
            sa.text("UPDATE exit_workflows SET is_active = false WHERE id = :i"),
            {"i": row.id},
        )
        await session.commit()
    await session.rollback()
