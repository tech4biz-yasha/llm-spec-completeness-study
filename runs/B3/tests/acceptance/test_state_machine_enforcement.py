"""states.yaml enforcement, forbidden list included.

AGENTS.md: "Every state transition validated against states.yaml, forbidden list included.
A forbidden transition raises, never silently no-ops."
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from exit_workflow.domain.states import state_machine
from exit_workflow.enums import Actor, WorkflowState
from exit_workflow.errors import ForbiddenTransition, NotAuthorized, WrongState
from exit_workflow.services.identity import Principal
from exit_workflow.services.transitions import apply_transition, load_for_update

from ..support import drive_to_damage_confirmed, initiate, status


@pytest.mark.parametrize(
    ("from_state", "to_state"),
    [
        (WorkflowState.INITIATED, WorkflowState.COMPLETE),
        (WorkflowState.DOCS_SUBMITTED, WorkflowState.REFUND_PROCESSED),
        (WorkflowState.INSPECTION_DONE, WorkflowState.REFUND_PROCESSED),
        (WorkflowState.STALLED, WorkflowState.COMPLETE),
    ],
)
def test_forbidden_pairs_raise(from_state, to_state):
    """Each pair is listed under states.yaml#exit_workflow.forbidden."""
    with pytest.raises(ForbiddenTransition):
        state_machine().validate(
            from_state=from_state,
            to_state=to_state,
            actor=Actor.SYSTEM,
            history={from_state},
        )


def test_noc_without_refund_processed_is_forbidden():
    """states.yaml: "any -> NOC_ISSUED without REFUND_PROCESSED" (T13 order, EXIT-08)."""
    with pytest.raises(ForbiddenTransition) as raised:
        state_machine().validate(
            from_state=WorkflowState.DAMAGE_CONFIRMED,
            to_state=WorkflowState.NOC_ISSUED,
            actor=Actor.SYSTEM,
            history={WorkflowState.DAMAGE_CONFIRMED},
        )
    assert "REFUND_PROCESSED" in raised.value.details["forbidden_rule"]


def test_noc_after_refund_processed_is_allowed():
    transition = state_machine().validate(
        from_state=WorkflowState.REFUND_PROCESSED,
        to_state=WorkflowState.NOC_ISSUED,
        actor=Actor.SYSTEM,
        history={WorkflowState.REFUND_PROCESSED},
    )
    assert transition.rule == "EXIT-08"


def test_unknown_transition_raises_wrong_state():
    with pytest.raises(WrongState):
        state_machine().validate(
            from_state=WorkflowState.OWNER_NOTIFIED,
            to_state=WorkflowState.DAMAGE_CONFIRMED,
            actor=Actor.OWNER,
            history={WorkflowState.OWNER_NOTIFIED},
        )


def test_wrong_actor_raises():
    """states.yaml names the actor of each transition."""
    with pytest.raises(WrongState):
        state_machine().validate(
            from_state=WorkflowState.INSPECTION_DONE,
            to_state=WorkflowState.DAMAGE_CONFIRMED,
            actor=Actor.TENANT,  # states.yaml says owner
            history={WorkflowState.INSPECTION_DONE},
        )


def test_missing_required_fields_raise():
    """states.yaml INITIATED -> DOCS_SUBMITTED requires move_out_date, reason, documents."""
    with pytest.raises(WrongState) as raised:
        state_machine().validate(
            from_state=WorkflowState.INITIATED,
            to_state=WorkflowState.DOCS_SUBMITTED,
            actor=Actor.TENANT,
            history={WorkflowState.INITIATED},
            provided=("reason",),
        )
    assert set(raised.value.details["missing"]) == {"move_out_date", "documents"}


def test_forbidden_transition_is_refused_at_the_database_too(
    service, session_factory, tenant, move_out_date
):
    """A forbidden transition raises rather than no-ops, even called directly."""
    result = initiate(service, tenant, move_out_date)
    from exit_workflow.db.session import transaction

    with pytest.raises(ForbiddenTransition), transaction(session_factory) as session:
        workflow = load_for_update(session, result.workflow_id)
        workflow.status = WorkflowState.STALLED.value
        session.flush()
        apply_transition(
            session,
            workflow,
            to_state=WorkflowState.COMPLETE,
            actor=Actor.SYSTEM,
            actor_id=None,
            occurred_at=service._now(),
        )
    # The transaction rolled back; the workflow is untouched.
    assert status(session_factory, result.workflow_id) is WorkflowState.DOCS_SUBMITTED


def test_settle_before_owner_confirmation_is_refused(
    service, session_factory, tenant, owner, agency, system, move_out_date
):
    """rules.yaml#EXIT-06 — owner confirmation is mandatory before settlement."""
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)
    service.schedule_inspection(result.workflow_id, principal=owner)
    service.submit_inspection_report(
        result.workflow_id, principal=agency, damage_amount=Decimal("10.00"), photos=["p"]
    )

    with pytest.raises(ForbiddenTransition):
        service.settle(result.workflow_id, principal=system)
    assert status(session_factory, result.workflow_id) is WorkflowState.INSPECTION_DONE


def test_out_of_order_endpoint_calls_are_409(
    client, move_out_date, tenant_headers, owner_headers, agency_headers
):
    """api.yaml: 409 WRONG_STATE."""
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

    # Confirming damage before an inspection report exists.
    response = client.post(f"/exit-workflows/{workflow_id}/confirm-damage", headers=owner_headers)
    assert response.status_code == 409
    assert response.json()["code"] == "WRONG_STATE"

    # Filing an inspection report before the inspection is scheduled.
    response = client.post(
        f"/exit-workflows/{workflow_id}/inspection-report",
        json={"damage_amount": "1.00", "photos": []},
        headers=agency_headers,
    )
    assert response.status_code == 409
    assert response.json()["code"] == "WRONG_STATE"


def test_roles_are_enforced_per_endpoint(
    client, move_out_date, tenant_headers, owner_headers, agency_headers
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

    # api.yaml: schedule-inspection is the owner's.
    assert (
        client.post(
            f"/exit-workflows/{workflow_id}/schedule-inspection", headers=tenant_headers
        ).status_code
        == 403
    )
    # api.yaml: settle is system|owner, not the agency.
    assert (
        client.post(f"/exit-workflows/{workflow_id}/settle", headers=agency_headers).status_code
        == 403
    )


def test_another_owner_cannot_act_on_the_workflow(service, tenant, move_out_date):
    result = initiate(service, tenant, move_out_date)
    service.notify_owner(result.workflow_id, result.outbox_event_id)
    stranger = Principal(user_id="USR-OWNER-2", role=Actor.OWNER)
    with pytest.raises(NotAuthorized):
        service.schedule_inspection(result.workflow_id, principal=stranger)


def test_settle_may_be_initiated_by_the_owner(service, tenant, owner, agency, move_out_date):
    """api.yaml settle authz: system|owner."""
    workflow_id = drive_to_damage_confirmed(
        service,
        tenant=tenant,
        owner=owner,
        agency=agency,
        move_out_date=move_out_date,
        damage_amount=Decimal("1.00"),
    )
    result = service.settle(workflow_id, principal=owner)
    assert result.status is WorkflowState.COMPLETE
