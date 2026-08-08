"""The property exit lock, as seen from outside this module.

rules.yaml#EXIT-03: the lock "blocks new contracts on the property (BR-1) and is released
only by workflow COMPLETE". edges.yaml#X-006: a new contract attempted on the property
mid-exit is refused with 409 EXIT_WORKFLOW_INCOMPLETE.

The contracting module calls ``assert_property_free_for_new_contract`` before creating a
contract. Scoping the BR-1 lock to a role versus an identity is risks.md#R1 and is not
decided here: this guard is about the *property*, which is what EXIT-03 states.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Property
from ..errors import ExitWorkflowIncomplete


def assert_property_free_for_new_contract(session: Session, property_id: str) -> None:
    """Raise 409 EXIT_WORKFLOW_INCOMPLETE when the property is exit-locked."""
    row = session.execute(
        select(Property.exit_lock, Property.exit_lock_workflow_id).where(Property.id == property_id)
    ).one_or_none()
    if row is None:
        return
    exit_lock, workflow_id = row
    if exit_lock:
        # rules.yaml#EXIT-03 (BR-1), edges.yaml#X-006
        raise ExitWorkflowIncomplete(
            f"property {property_id} has an incomplete exit workflow",
            property_id=property_id,
            workflow_id=workflow_id,
        )
