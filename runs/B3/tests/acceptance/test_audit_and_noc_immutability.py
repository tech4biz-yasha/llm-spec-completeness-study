"""rules.yaml#EXIT-10 (append-only audit) and #EXIT-09 (immutable NOC).

AGENTS.md: "Audit rows are append-only. Enforced by DB trigger, not application code."
These tests bypass the application entirely and attack the tables with raw SQL.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import sqlalchemy as sa

from exit_workflow.enums import WorkflowState

from ..support import audit_trail, drive_to_damage_confirmed, initiate


def test_audit_rows_cannot_be_updated(service, engine, tenant, move_out_date):
    initiate(service, tenant, move_out_date)
    with pytest.raises(sa.exc.DBAPIError) as raised, engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE exit_workflow_audit SET to_state = 'COMPLETE' WHERE id > 0")
        )
    assert "append-only" in str(raised.value)


def test_audit_rows_cannot_be_deleted(service, engine, tenant, move_out_date):
    initiate(service, tenant, move_out_date)
    with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM exit_workflow_audit"))


def test_audit_table_cannot_be_truncated(service, engine, tenant, move_out_date):
    initiate(service, tenant, move_out_date)
    with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE exit_workflow_audit"))


def test_audit_rows_carry_actor_timestamp_from_to_and_metadata(
    service, session_factory, tenant, move_out_date
):
    """rules.yaml#EXIT-10."""
    result = initiate(service, tenant, move_out_date)
    trail = audit_trail(session_factory, result.workflow_id)

    creation, docs = trail
    assert creation.from_state is None
    assert creation.to_state == WorkflowState.INITIATED
    assert creation.actor_role == "tenant"
    assert creation.actor_id == tenant.user_id
    assert creation.created_at is not None

    assert docs.from_state == WorkflowState.INITIATED
    assert docs.to_state == WorkflowState.DOCS_SUBMITTED
    assert docs.rule_id == "EXIT-02"
    assert docs.meta["reason"] == "END_OF_TENANCY"
    assert docs.meta["document_count"] == 1


def test_issued_noc_row_is_immutable(service, engine, tenant, owner, agency, system, move_out_date):
    """rules.yaml#EXIT-09 — "immutable once issued"."""
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("5.00"),
    )
    service.settle(workflow_id, principal=system)

    with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
        connection.execute(sa.text("UPDATE noc_documents SET object_key = 'x'"))
    with pytest.raises(sa.exc.DBAPIError), engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM noc_documents"))


def test_stored_noc_object_cannot_be_overwritten(
    service, storage, tenant, owner, agency, system, move_out_date
):
    """rules.yaml#EXIT-09 — the object store refuses a second write to the same key."""
    from exit_workflow.ports.storage import StorageError

    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("5.00"),
    )
    service.settle(workflow_id, principal=system)
    (bucket, key), body = next(iter(storage.objects.items()))

    with pytest.raises(StorageError):
        storage.put_immutable(
            bucket=bucket, key=key, body=b"tampered", content_type="application/pdf"
        )
    assert storage.objects[(bucket, key)] == body
