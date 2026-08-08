"""edges.yaml#X-001 — duplicate initiation. rules.yaml#EXIT-01."""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import func, select

from exit_workflow.db.models import ExitWorkflow
from exit_workflow.errors import ExitAlreadyInProgress

from ..conftest import CONTRACT_ID
from ..support import initiate


def test_x001(client, session_factory, move_out_date, tenant_headers):
    """409 EXIT_ALREADY_IN_PROGRESS with the existing workflow_id. Never a second."""
    payload = {
        "contract_id": CONTRACT_ID,
        "move_out_date": move_out_date.isoformat(),
        "reason": "END_OF_TENANCY",
        "documents": [{"id": "DOC-1"}],
    }
    first = client.post("/exit-workflows", json=payload, headers=tenant_headers)
    assert first.status_code == 201
    existing_id = first.json()["workflow_id"]

    second = client.post("/exit-workflows", json=payload, headers=tenant_headers)
    assert second.status_code == 409
    body = second.json()
    assert body["code"] == "EXIT_ALREADY_IN_PROGRESS"
    assert body["details"]["workflow_id"] == existing_id

    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(ExitWorkflow)).scalar_one() == 1


def test_x001_concurrent_initiation_creates_exactly_one_workflow(
    service, session_factory, tenant, move_out_date
):
    """Two initiations racing on one contract. The UNIQUE constraint is the guarantee."""
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def attempt() -> None:
        barrier.wait(timeout=10)
        try:
            outcomes.append(initiate(service, tenant, move_out_date))
        except Exception as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    successes = [o for o in outcomes if not isinstance(o, Exception)]
    failures = [o for o in outcomes if isinstance(o, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ExitAlreadyInProgress)
    assert failures[0].code == "EXIT_ALREADY_IN_PROGRESS"

    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(ExitWorkflow)).scalar_one() == 1


def test_x001_service_raises_with_the_existing_id(service, tenant, move_out_date):
    first = initiate(service, tenant, move_out_date)
    with pytest.raises(ExitAlreadyInProgress) as raised:
        initiate(service, tenant, move_out_date)
    assert raised.value.details["workflow_id"] == first.workflow_id
