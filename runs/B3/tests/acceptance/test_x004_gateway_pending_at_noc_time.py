"""edges.yaml#X-004 — refund still PENDING when NOC generation is attempted.

rules.yaml#EXIT-08: NOC is generated ONLY after the gateway confirms SUCCEEDED.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from exit_workflow.db.models import NocDocument
from exit_workflow.enums import PaymentStatus, WorkflowState
from exit_workflow.errors import PaymentPendingError
from exit_workflow.ports.payments import GatewayResult

from ..support import drive_to_damage_confirmed, status


def test_x004(service, session_factory, gateway, tenant, owner, agency, system, move_out_date):
    """Refuse. NOC only after SUCCEEDED."""
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("100.00"),
    )
    gateway.set_status(workflow_id, GatewayResult(status=PaymentStatus.PENDING, reference="gw-1"))

    with pytest.raises(PaymentPendingError) as raised:
        service.settle(workflow_id, principal=system)
    assert raised.value.code == "PAYMENT_PENDING"
    assert raised.value.http_status == 409

    # The refund was created (states.yaml DAMAGE_CONFIRMED -> REFUND_PROCESSED) but the
    # workflow holds there: no NOC_ISSUED, no COMPLETE.
    assert status(session_factory, workflow_id) is WorkflowState.REFUND_PROCESSED
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(NocDocument)).scalar_one() == 0


def test_x004_failed_payment_also_holds(
    service, session_factory, gateway, tenant, owner, agency, system, move_out_date
):
    """algorithm.md#11 — "PENDING or FAILED -> hold, never proceed"."""
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("100.00"),
    )
    gateway.set_status(
        workflow_id,
        GatewayResult(status=PaymentStatus.FAILED, failure_reason="beneficiary rejected"),
    )
    with pytest.raises(PaymentPendingError):
        service.settle(workflow_id, principal=system)
    assert status(session_factory, workflow_id) is WorkflowState.REFUND_PROCESSED
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(NocDocument)).scalar_one() == 0


def test_x004_completes_once_the_gateway_succeeds(
    service, session_factory, gateway, tenant, owner, agency, system, move_out_date
):
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("100.00"),
    )
    gateway.set_status(workflow_id, GatewayResult(status=PaymentStatus.PENDING, reference="gw-1"))
    with pytest.raises(PaymentPendingError):
        service.settle(workflow_id, principal=system)

    gateway.set_status(workflow_id, GatewayResult(status=PaymentStatus.SUCCEEDED, reference="gw-1"))
    result = service.settle(workflow_id, principal=system)

    assert result.status is WorkflowState.COMPLETE
    assert result.refund_amount == Decimal("9900.00")
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(NocDocument)).scalar_one() == 1


def test_x004_over_http_is_409_payment_pending(
    client,
    session_factory,
    gateway,
    move_out_date,
    tenant_headers,
    owner_headers,
    agency_headers,
    system_headers,
):
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
    gateway.set_status(workflow_id, GatewayResult(status=PaymentStatus.PENDING))
    client.post(f"/exit-workflows/{workflow_id}/schedule-inspection", headers=owner_headers)
    client.post(
        f"/exit-workflows/{workflow_id}/inspection-report",
        json={"damage_amount": "100.00", "photos": ["p1"]},
        headers=agency_headers,
    )
    client.post(f"/exit-workflows/{workflow_id}/confirm-damage", headers=owner_headers)

    response = client.post(f"/exit-workflows/{workflow_id}/settle", headers=system_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "PAYMENT_PENDING"
