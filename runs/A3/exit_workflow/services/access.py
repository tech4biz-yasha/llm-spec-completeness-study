"""Row-level authorisation.

Role alone is never enough: an owner may act on *their* exits, a tenant on
*their own*, an agency only on inspections routed to it.
"""

from __future__ import annotations

from exit_workflow.core.errors import ForbiddenError, NotFoundError
from exit_workflow.core.security import Principal, Role
from exit_workflow.models.inspection import Inspection
from exit_workflow.models.workflow import ExitWorkflow


def is_participant(workflow: ExitWorkflow, principal: Principal) -> bool:
    match principal.role:
        case Role.ADMIN | Role.SERVICE:
            return True
        case Role.TENANT:
            return principal.subject_id == workflow.tenant_id
        case Role.OWNER:
            return principal.subject_id == workflow.owner_id
        case Role.INSPECTION_AGENCY:
            return any(i.agency_id == principal.org_id for i in workflow.inspections)
    return False  # pragma: no cover - exhaustive above


def ensure_can_view(workflow: ExitWorkflow, principal: Principal) -> None:
    """404 rather than 403 for non-participants: existence is not disclosed."""

    if not is_participant(workflow, principal):
        raise NotFoundError("Exit workflow not found.")


def ensure_is_tenant(workflow: ExitWorkflow, principal: Principal) -> None:
    if principal.is_admin:
        return
    if principal.role is not Role.TENANT or principal.subject_id != workflow.tenant_id:
        raise ForbiddenError("Only the tenant on this exit workflow may perform this action.")


def ensure_is_owner(workflow: ExitWorkflow, principal: Principal) -> None:
    if principal.is_admin:
        return
    if principal.role is not Role.OWNER or principal.subject_id != workflow.owner_id:
        raise ForbiddenError("Only the property owner may perform this action.")


def ensure_is_party(workflow: ExitWorkflow, principal: Principal) -> None:
    """Owner or tenant (either may pick an inspection slot, per O15)."""

    if principal.is_admin:
        return
    if principal.subject_id not in workflow.party_ids():
        raise ForbiddenError("Only the tenant or the owner may perform this action.")


def ensure_is_assigned_agency(inspection: Inspection, principal: Principal) -> None:
    if principal.is_admin:
        return
    if principal.role is not Role.INSPECTION_AGENCY:
        raise ForbiddenError("Only the assigned inspection agency may perform this action.")
    if inspection.agency_id != principal.agency_scope():
        raise ForbiddenError("This inspection is assigned to a different agency.")
