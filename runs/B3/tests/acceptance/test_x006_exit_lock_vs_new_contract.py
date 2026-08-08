"""edges.yaml#X-006 — new contract attempted on the property mid-exit.

rules.yaml#EXIT-03 (BR-1): 409 EXIT_WORKFLOW_INCOMPLETE.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from exit_workflow.errors import ExitWorkflowIncomplete
from exit_workflow.services.exit_lock import assert_property_free_for_new_contract

from ..conftest import PROPERTY_ID
from ..support import drive_to_damage_confirmed, initiate


def test_x006(service, session_factory, tenant, move_out_date):
    with session_factory() as session:
        # Before initiation the property is free.
        assert_property_free_for_new_contract(session, PROPERTY_ID)

    initiate(service, tenant, move_out_date)

    with session_factory() as session, pytest.raises(ExitWorkflowIncomplete) as raised:
        assert_property_free_for_new_contract(session, PROPERTY_ID)

    assert raised.value.code == "EXIT_WORKFLOW_INCOMPLETE"
    assert raised.value.http_status == 409
    assert raised.value.details["property_id"] == PROPERTY_ID


def test_x006_lock_is_released_only_by_complete(
    service, session_factory, tenant, owner, agency, system, move_out_date
):
    """rules.yaml#EXIT-03 — "released only by workflow COMPLETE"."""
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("10.00"),
    )
    with session_factory() as session, pytest.raises(ExitWorkflowIncomplete):
        assert_property_free_for_new_contract(session, PROPERTY_ID)

    service.settle(workflow_id, principal=system)

    with session_factory() as session:
        assert_property_free_for_new_contract(session, PROPERTY_ID)


def test_x006_unknown_property_is_not_blocked(session_factory):
    with session_factory() as session:
        assert_property_free_for_new_contract(session, "PROP-UNKNOWN")
