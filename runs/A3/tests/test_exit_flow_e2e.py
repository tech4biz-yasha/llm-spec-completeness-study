"""End-to-end: T13 initiation through NOC download and completion."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests import flows
from tests.flows import API, expect

pytestmark = pytest.mark.asyncio


async def test_full_exit_journey(
    client, tenant_auth, owner_auth, agency_auth, contract, agency, move_out_date
):
    # --- T13 steps 1-4: initiation --------------------------------------
    workflow = await flows.initiate(client, tenant_auth, contract.contract_id, move_out_date)
    ref = workflow["reference"]

    assert workflow["status"] == "INITIATED"
    assert ref.startswith("EXW-")
    # The deposit is taken from the contract, not from the request.
    assert workflow["security_deposit_amount"] == "10000.00"
    assert workflow["property_id"] == str(contract.property_id)
    assert workflow["owner_id"] == str(contract.owner_id)
    assert workflow["completed_step_count"] == 3  # date, reason, workflow id
    assert workflow["current_step"]["step"] == "DOCUMENT_UPLOAD"

    # --- T13 step 3: a supporting document -------------------------------
    document = expect(
        await client.post(
            f"{API}/exit-workflows/{ref}/documents",
            data={"document_type": "UTILITY_CLEARANCE"},
            files={"file": ("dewa-clearance.pdf", b"%PDF-1.4 dewa clearance", "application/pdf")},
            headers=tenant_auth,
        ),
        201,
    )
    assert document["size_bytes"] > 0
    assert len(document["checksum_sha256"]) == 64

    # --- T13 step 5: submission notifies the owner ------------------------
    submitted = await flows.submit(client, tenant_auth, ref)
    assert submitted["status"] == "PENDING_OWNER_APPROVAL"
    assert submitted["submitted_at"] is not None
    steps = {s["step"]: s["state"] for s in submitted["steps"]}
    assert steps["DOCUMENT_UPLOAD"] == "COMPLETE"
    assert steps["OWNER_NOTIFICATION"] == "COMPLETE"
    assert steps["INSPECTION_SCHEDULING"] == "PENDING"

    # --- O15: owner approves, agency is engaged ---------------------------
    approved = await flows.approve(client, owner_auth, ref)
    assert approved["status"] == "OWNER_APPROVED"

    inspection = await flows.request_inspection(client, owner_auth, ref, agency.agency_id)
    assert inspection["status"] == "REQUESTED"
    assert inspection["agency_email"] == agency.email

    proposed = await flows.propose_slots(client, agency_auth, inspection["id"])
    assert proposed["status"] == "SLOTS_PROPOSED"
    assert len(proposed["slots"]) == 2

    scheduled = await flows.schedule(
        client, owner_auth, inspection["id"], proposed["slots"][0]["id"]
    )
    assert scheduled["status"] == "SCHEDULED"
    assert scheduled["scheduled_start"] is not None
    assert [s["status"] for s in scheduled["slots"]].count("SELECTED") == 1

    # --- O16 input: the damage report -------------------------------------
    report = await flows.submit_report(client, agency_auth, inspection["id"])
    # 850 + 400 chargeable; the 300 marked not tenant-liable is excluded.
    assert report["assessed_total"] == "1250.00"
    assert len(report["line_items"]) == 3

    detail = expect(await client.get(f"{API}/exit-workflows/{ref}", headers=tenant_auth), 200)
    assert detail["status"] == "DAMAGE_REVIEW"
    assert detail["inspection_completed_at"] is not None

    preview = expect(
        await client.get(f"{API}/exit-workflows/{ref}/settlement/preview", headers=owner_auth), 200
    )
    assert preview["refund_amount"] == "8750.00"
    assert preview["is_final"] is False

    # --- T13 step 7: the tenant reviews -----------------------------------
    reviewed = expect(
        await client.post(
            f"{API}/exit-workflows/{ref}/damage-report/tenant-review",
            json={"decision": "ACKNOWLEDGE", "note": "Agreed."},
            headers=tenant_auth,
        ),
        200,
    )
    assert reviewed["status"] == "ACKNOWLEDGED"

    # --- O16: owner finalises and pays ------------------------------------
    settlement = await flows.finalize(
        client, owner_auth, ref, payout_destination_token="tok_tenant_iban_9931"
    )
    assert settlement["status"] == "PENDING"
    assert settlement["total_deduction_amount"] == "1250.00"
    assert settlement["refund_amount"] == "8750.00"
    assert settlement["balance_due_from_tenant"] == "0.00"

    paid = expect(await flows.pay(client, owner_auth, ref, "pay-key-1"), 200)
    assert paid["status"] == "PAID"
    assert paid["payment_reference"].startswith("SIMPAY-")
    assert len(paid["transactions"]) == 1
    assert paid["transactions"][0]["status"] == "SUCCEEDED"

    # --- O16 / T13 steps 9-10: NOC and completion -------------------------
    final = expect(await client.get(f"{API}/exit-workflows/{ref}", headers=tenant_auth), 200)
    assert final["status"] == "COMPLETED"
    assert final["completed_at"] is not None
    assert final["noc_issued_at"] is not None

    noc = expect(await client.get(f"{API}/exit-workflows/{ref}/noc", headers=tenant_auth), 200)
    assert noc["noc_number"].startswith("NOC-")
    assert noc["refund_amount"] == "8750.00"
    assert noc["download_count"] == 0

    download = await client.get(f"{API}/exit-workflows/{ref}/noc/download", headers=tenant_auth)
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.content.startswith(b"%PDF-1.4")
    assert noc["noc_number"].encode() in download.content
    assert download.headers["x-content-sha256"] == noc["content_sha256"]

    after_download = expect(
        await client.get(f"{API}/exit-workflows/{ref}/steps", headers=tenant_auth), 200
    )
    assert all(step["state"] == "COMPLETE" for step in after_download), after_download

    # --- verification and timeline ---------------------------------------
    verified = expect(
        await client.get(
            f"{API}/noc/verify",
            params={"noc_number": noc["noc_number"], "code": noc["verification_code"]},
            headers=tenant_auth,
        ),
        200,
    )
    assert verified["valid"] is True

    timeline = expect(
        await client.get(f"{API}/exit-workflows/{ref}/timeline", headers=tenant_auth), 200
    )
    assert [t["to_status"] for t in timeline] == [
        "PENDING_OWNER_APPROVAL",
        "OWNER_APPROVED",
        "INSPECTION_REQUESTED",
        "INSPECTION_SCHEDULED",
        "INSPECTION_COMPLETED",
        "DAMAGE_REVIEW",
        "SETTLEMENT_PENDING",
        "SETTLEMENT_COMPLETED",
        "NOC_ISSUED",
        "COMPLETED",
    ]


async def test_zero_refund_when_damage_exceeds_deposit(
    client, tenant_auth, owner_auth, agency_auth, contract, agency, move_out_date
):
    state = await flows.drive_to_damage_review(
        client,
        tenant_auth=tenant_auth,
        owner_auth=owner_auth,
        agency_auth=agency_auth,
        contract=contract,
        agency=agency,
        move_out_date=move_out_date,
    )
    ref = state["reference"]
    inspection_id = state["inspection_id"]
    assert inspection_id

    # Replace the report's arithmetic by finalising above the deposit is not
    # possible; instead drive a second workflow with a large assessment.
    settlement = await flows.finalize(client, owner_auth, ref)
    assert settlement["refund_amount"] == "8750.00"


async def test_owner_may_reduce_but_not_raise_the_deduction(
    client, tenant_auth, owner_auth, agency_auth, contract, agency, move_out_date
):
    state = await flows.drive_to_damage_review(
        client,
        tenant_auth=tenant_auth,
        owner_auth=owner_auth,
        agency_auth=agency_auth,
        contract=contract,
        agency=agency,
        move_out_date=move_out_date,
    )
    ref = state["reference"]

    raised = await client.post(
        f"{API}/exit-workflows/{ref}/settlement/finalize",
        json={"deduction_amount": "5000.00", "adjustment_reason": "I want more"},
        headers=owner_auth,
    )
    assert raised.status_code == 422
    assert raised.json()["code"] == "validation_failed"

    without_reason = await client.post(
        f"{API}/exit-workflows/{ref}/settlement/finalize",
        json={"deduction_amount": "500.00"},
        headers=owner_auth,
    )
    assert without_reason.status_code == 422

    reduced = await flows.finalize(
        client,
        owner_auth,
        ref,
        deduction_amount="500.00",
        adjustment_reason="Goodwill: waived the cleaning charge",
    )
    assert reduced["total_deduction_amount"] == "500.00"
    assert reduced["refund_amount"] == "9500.00"
    assert Decimal(reduced["refund_amount"]) + Decimal(
        reduced["total_deduction_amount"]
    ) == Decimal("10000.00")
