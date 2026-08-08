"""rules.yaml#EXIT-01, #EXIT-02, #EXIT-03 and the api.yaml 422 vocabulary."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from exit_workflow.db.models import Contract, ExitWorkflow, Property
from exit_workflow.enums import Actor, WorkflowState
from exit_workflow.errors import (
    ContractNotActive,
    DocumentsRequired,
    MoveOutDateInPast,
    NotAuthorized,
    ReasonInvalid,
)
from exit_workflow.services.identity import Principal

from ..conftest import CONTRACT_ID, PROPERTY_ID, TENANT_ID
from ..support import initiate


def test_move_out_date_in_past_is_422(client, move_out_date, tenant_headers):
    response = client.post(
        "/exit-workflows",
        json={
            "contract_id": CONTRACT_ID,
            "move_out_date": (move_out_date - timedelta(days=30)).isoformat(),
            "reason": "END_OF_TENANCY",
            "documents": [{"id": "DOC-1"}],
        },
        headers=tenant_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "MOVE_OUT_DATE_IN_PAST"


def test_reason_must_come_from_the_reference_list(client, move_out_date, tenant_headers):
    """rules.yaml#EXIT-02. The list itself is deployment-supplied — blockers.md#B-2."""
    response = client.post(
        "/exit-workflows",
        json={
            "contract_id": CONTRACT_ID,
            "move_out_date": move_out_date.isoformat(),
            "reason": "BECAUSE_I_FELT_LIKE_IT",
            "documents": [{"id": "DOC-1"}],
        },
        headers=tenant_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "REASON_INVALID"


def test_at_least_one_document_is_required(client, move_out_date, tenant_headers):
    response = client.post(
        "/exit-workflows",
        json={
            "contract_id": CONTRACT_ID,
            "move_out_date": move_out_date.isoformat(),
            "reason": "END_OF_TENANCY",
            "documents": [],
        },
        headers=tenant_headers,
    )
    assert response.status_code == 422
    assert response.json()["code"] == "DOCUMENTS_REQUIRED"


def test_validation_order_follows_api_yaml(service, tenant, move_out_date):
    """api.yaml lists MOVE_OUT_DATE_IN_PAST | REASON_INVALID | DOCUMENTS_REQUIRED."""
    with pytest.raises(MoveOutDateInPast):
        service.initiate(
            principal=tenant,
            contract_id=CONTRACT_ID,
            move_out_date=move_out_date - timedelta(days=1000),
            reason="NOT_A_REASON",
            documents=[],
        )
    with pytest.raises(ReasonInvalid):
        service.initiate(
            principal=tenant,
            contract_id=CONTRACT_ID,
            move_out_date=move_out_date,
            reason="NOT_A_REASON",
            documents=[],
        )
    with pytest.raises(DocumentsRequired):
        service.initiate(
            principal=tenant,
            contract_id=CONTRACT_ID,
            move_out_date=move_out_date,
            reason="END_OF_TENANCY",
            documents=[],
        )


def test_contract_must_be_active(service, session_factory, tenant, move_out_date):
    """algorithm.md#1 — assert status == ACTIVE, else 422. rules.yaml#EXIT-01."""
    with session_factory() as session:
        session.get(Contract, CONTRACT_ID).status = "EXPIRED"
        session.commit()

    with pytest.raises(ContractNotActive) as raised:
        initiate(service, tenant, move_out_date)
    assert raised.value.http_status == 422
    assert raised.value.code == "WRONG_STATE"


def test_tenant_may_only_exit_their_own_contract(service, move_out_date):
    """api.yaml authz: "tenant, own active contract only"."""
    stranger = Principal(user_id="USR-TENANT-2", role=Actor.TENANT)
    with pytest.raises(NotAuthorized):
        initiate(service, stranger, move_out_date)


def test_owner_may_not_initiate(service, owner, move_out_date):
    with pytest.raises(NotAuthorized):
        initiate(service, owner, move_out_date)


def test_failed_validation_leaves_no_lock_and_no_workflow(
    service, session_factory, tenant, move_out_date
):
    """rules.yaml#EXIT-03 — the lock and the insert share a transaction, so neither
    survives a rejected initiation."""
    with pytest.raises(DocumentsRequired):
        service.initiate(
            principal=tenant,
            contract_id=CONTRACT_ID,
            move_out_date=move_out_date,
            reason="END_OF_TENANCY",
            documents=[],
        )
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(ExitWorkflow)).scalar_one() == 0
        assert session.get(Property, PROPERTY_ID).exit_lock is False


def test_workflow_ids_are_sequential(service, tenant, session_factory, move_out_date):
    """rules.yaml#EXIT-02 — NNNNN from a PostgreSQL sequence."""
    first = initiate(service, tenant, move_out_date)
    with session_factory() as session:
        session.add(
            Contract(
                id="CON-2",
                property_id=PROPERTY_ID,
                tenant_id=TENANT_ID,
                owner_id="USR-OWNER-1",
                status="ACTIVE",
                security_deposit_minor=100_000,
                currency="AED",
            )
        )
        session.commit()
    second = initiate(service, tenant, move_out_date, contract_id="CON-2")

    assert first.workflow_id == "EX-20260301-00001"
    assert second.workflow_id == "EX-20260301-00002"


def test_initiation_state_is_docs_submitted(service, session_factory, tenant, move_out_date):
    """algorithm.md#4 — the transaction commits at DOCS_SUBMITTED, not INITIATED."""
    result = initiate(service, tenant, move_out_date)
    assert result.status is WorkflowState.DOCS_SUBMITTED
