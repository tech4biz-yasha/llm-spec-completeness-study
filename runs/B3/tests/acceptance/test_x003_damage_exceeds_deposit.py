"""edges.yaml#X-003 — confirmed damage exceeds the deposit. rules.yaml#EXIT-07, risks.md#R8."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from exit_workflow.db.models import NocDocument, Payment
from exit_workflow.enums import WorkflowState
from exit_workflow.errors import SpecUnresolved

from ..conftest import DEPOSIT
from ..support import drive_to_damage_confirmed, status


def test_x003(service, session_factory, tenant, owner, agency, system, move_out_date):
    """raise SpecUnresolved("R8"). No refund, no NOC, workflow holds at DAMAGE_CONFIRMED."""
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=DEPOSIT + Decimal("0.01"),
    )

    with pytest.raises(SpecUnresolved) as raised:
        service.settle(workflow_id, principal=system)

    assert raised.value.blocker == "R8"
    assert raised.value.code == "SPEC_UNRESOLVED_R8"

    assert status(session_factory, workflow_id) is WorkflowState.DAMAGE_CONFIRMED
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(Payment)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(NocDocument)).scalar_one() == 0


def test_x003_over_http_is_501_spec_unresolved_r8(
    client,
    session_factory,
    move_out_date,
    tenant_headers,
    owner_headers,
    agency_headers,
    system_headers,
):
    """api.yaml: settle 501 SPEC_UNRESOLVED_R8."""
    from ..conftest import CONTRACT_ID

    created = client.post(
        "/exit-workflows",
        json={
            "contract_id": CONTRACT_ID,
            "move_out_date": move_out_date.isoformat(),
            "reason": "END_OF_TENANCY",
            "documents": [{"id": "DOC-1"}],
        },
        headers=tenant_headers,
    )
    workflow_id = created.json()["workflow_id"]
    client.post(f"/exit-workflows/{workflow_id}/schedule-inspection", headers=owner_headers)
    client.post(
        f"/exit-workflows/{workflow_id}/inspection-report",
        json={"damage_amount": str(DEPOSIT + Decimal("500.00")), "photos": ["p1"]},
        headers=agency_headers,
    )
    client.post(f"/exit-workflows/{workflow_id}/confirm-damage", headers=owner_headers)

    response = client.post(f"/exit-workflows/{workflow_id}/settle", headers=system_headers)
    assert response.status_code == 501
    body = response.json()
    assert body["code"] == "SPEC_UNRESOLVED_R8"
    assert body["blocker"] == "R8"

    # The workflow holds; nothing was written.
    assert status(session_factory, workflow_id) is WorkflowState.DAMAGE_CONFIRMED


def test_x003_retrying_settle_stays_blocked(
    service, session_factory, tenant, owner, agency, system, move_out_date
):
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=DEPOSIT * 2,
    )
    for _ in range(3):
        with pytest.raises(SpecUnresolved):
            service.settle(workflow_id, principal=system)
    assert status(session_factory, workflow_id) is WorkflowState.DAMAGE_CONFIRMED
