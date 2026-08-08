"""edges.yaml#X-005 — two settlement attempts race on one workflow.

"Idempotency key = workflow_id means one payment, second call returns existing."
rules.yaml#EXIT-08.
"""

from __future__ import annotations

import threading
from decimal import Decimal

from sqlalchemy import func, select

from exit_workflow.db.models import ExitWorkflowAudit, NocDocument, Payment
from exit_workflow.enums import WorkflowState

from ..support import drive_to_damage_confirmed, status


def test_x005(service, session_factory, gateway, tenant, owner, agency, system, move_out_date):
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("250.25"),
    )

    barrier = threading.Barrier(2)
    results: list[object] = []

    def attempt() -> None:
        barrier.wait(timeout=10)
        try:
            results.append(service.settle(workflow_id, principal=system))
        except Exception as exc:
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 2
    assert not [r for r in results if isinstance(r, Exception)], results

    payment_ids = {r.payment_id for r in results}
    refunds = {r.refund_amount for r in results}
    assert len(payment_ids) == 1
    assert refunds == {Decimal("9749.75")}

    with session_factory() as session:
        # One payment, one NOC, one COMPLETE.
        assert session.execute(select(func.count()).select_from(Payment)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(NocDocument)).scalar_one() == 1
        completes = session.execute(
            select(func.count())
            .select_from(ExitWorkflowAudit)
            .where(ExitWorkflowAudit.to_state == WorkflowState.COMPLETE.value)
        ).scalar_one()
        assert completes == 1
    assert status(session_factory, workflow_id) is WorkflowState.COMPLETE


def test_x005_sequential_resettle_returns_the_existing_payment(
    service, session_factory, gateway, tenant, owner, agency, system, move_out_date
):
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("250.25"),
    )
    first = service.settle(workflow_id, principal=system)
    second = service.settle(workflow_id, principal=system)

    assert first.payment_id == second.payment_id
    assert first.refund_amount == second.refund_amount
    assert second.status is WorkflowState.COMPLETE
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(Payment)).scalar_one() == 1
        assert session.execute(select(func.count()).select_from(NocDocument)).scalar_one() == 1
    # rules.yaml#EXIT-08 — the gateway saw the same idempotency key both times.
    assert gateway.calls == [workflow_id, workflow_id]
