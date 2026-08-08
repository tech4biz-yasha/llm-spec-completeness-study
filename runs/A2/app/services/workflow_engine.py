"""The single place where an exit workflow changes state.

Every command in the module funnels through :meth:`WorkflowEngine.transition`, which
validates the move against the state machine, applies role authorisation, stamps the
lifecycle timestamps, appends the state-history row, writes the audit entry and records
the outbox events -- all inside the caller's transaction. Nothing else is allowed to
assign ``ExitWorkflow.state``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.core.errors import AuthorizationError, ConflictError
from app.domain import events as ev
from app.domain.enums import TERMINAL_STATES, ActorRole, ExitWorkflowState
from app.domain.events import DomainEvent
from app.domain.state_machine import Transition, assert_transition_allowed
from app.models.exit_workflow import ExitWorkflow, StateTransition
from app.services.audit import AuditService
from app.services.events import EventRecorder

#: Actions whose target state is terminal but not a successful completion.
_CLOSURE_ACTIONS = {"cancel", "owner_reject", "expire"}


class WorkflowEngine:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        audit: AuditService,
        events: EventRecorder,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._audit = audit
        self._events = events

    # ------------------------------------------------------ authorisation
    def authorise_party(
        self,
        workflow: ExitWorkflow,
        ctx: RequestContext,
        *,
        agency_id: uuid.UUID | None = None,
        action: str = "access",
    ) -> None:
        """Confirm the caller is a party to this workflow.

        Role alone is not enough: an owner may only act on *their* workflows, and an
        agency only on the inspection it was actually engaged for.
        """
        principal = ctx.principal
        if principal.role in (ActorRole.ADMIN, ActorRole.SYSTEM):
            return
        if principal.role is ActorRole.TENANT and workflow.tenant_id == principal.actor_id:
            return
        if principal.role is ActorRole.OWNER and workflow.owner_id == principal.actor_id:
            return
        if (
            principal.role is ActorRole.INSPECTION_AGENCY
            and agency_id is not None
            and principal.agency_id == agency_id
        ):
            return
        raise AuthorizationError(
            "You are not a party to this exit workflow.",
            details={"workflow_id": str(workflow.id), "action": action},
        )

    # --------------------------------------------------------- transition
    def transition(
        self,
        workflow: ExitWorkflow,
        *,
        action: str,
        ctx: RequestContext,
        note: str | None = None,
        event_payload: dict[str, Any] | None = None,
        event_type: str | None = None,
        audit_changes: dict[str, Any] | None = None,
        extra_events: list[DomainEvent] | None = None,
    ) -> Transition:
        """Move ``workflow`` along ``action``.

        Raises :class:`IllegalTransitionError` when the action is not defined for the
        current state and :class:`AuthorizationError` when the caller's role may not
        trigger it.
        """
        if workflow.state in TERMINAL_STATES:
            raise ConflictError(
                f"This exit workflow is already {workflow.state.value.lower()} and "
                "cannot be changed.",
                code="workflow_closed",
                details={"state": workflow.state.value},
            )

        transition = assert_transition_allowed(workflow.state, action, ctx.principal.role)
        now = self._clock.now()
        previous = workflow.state

        workflow.state = transition.target
        self._stamp(workflow, transition, ctx, now, note)

        self._session.add(
            StateTransition(
                workflow_id=workflow.id,
                from_state=previous,
                to_state=transition.target,
                action=action,
                actor_id=ctx.principal.actor_id,
                actor_role=ctx.principal.role,
                note=note,
                occurred_at=now,
                request_id=ctx.request_id,
            )
        )

        self._audit.record(
            ctx,
            action=action,
            entity_type="exit_workflow",
            entity_id=workflow.id,
            workflow=workflow,
            from_state=previous.value,
            to_state=transition.target.value,
            changes=audit_changes or {},
            context={"note": note} if note else {},
        )

        payload = {
            "workflow_id": str(workflow.id),
            "reference": workflow.reference,
            "property_id": str(workflow.property_id),
            "contract_id": str(workflow.contract_id),
            "tenant_id": str(workflow.tenant_id),
            "owner_id": str(workflow.owner_id),
            "action": action,
            "from_state": previous.value,
            "to_state": transition.target.value,
            **(event_payload or {}),
        }
        self._events.record(
            DomainEvent(
                event_type=event_type or ev.STATE_CHANGED,
                workflow_id=workflow.id,
                payload=payload,
            ),
            ctx=ctx,
        )
        for extra in extra_events or []:
            self._events.record(extra, ctx=ctx)

        return transition

    def _stamp(
        self,
        workflow: ExitWorkflow,
        transition: Transition,
        ctx: RequestContext,
        now: Any,
        note: str | None,
    ) -> None:
        target = transition.target
        action = transition.action

        if action == "submit":
            workflow.submitted_at = now
        elif action == "owner_approve":
            workflow.owner_decided_at = now
            workflow.owner_decision_by = ctx.principal.actor_id
        elif action == "owner_reject":
            workflow.owner_decided_at = now
            workflow.owner_decision_by = ctx.principal.actor_id
            workflow.rejection_reason = note
        elif action == "withdraw":
            # Back to draft: the tenant may revise and resubmit. Clear the owner's
            # decision so a stale approval cannot be attributed to the new submission.
            workflow.submitted_at = None
            workflow.owner_notified_at = None
            workflow.owner_decided_at = None
            workflow.owner_decision_by = None
        elif action == "issue_noc":
            workflow.noc_issued_at = now
        elif action == "complete":
            workflow.completed_at = now

        if target in TERMINAL_STATES:
            workflow.closed_at = now
            workflow.closed_by = ctx.principal.actor_id
            if action in _CLOSURE_ACTIONS:
                workflow.closure_reason = note

    # ---------------------------------------------------------- helpers
    def record_event(
        self, event: DomainEvent, ctx: RequestContext | None = None
    ) -> None:
        self._events.record(event, ctx=ctx)

    def audit(
        self,
        ctx: RequestContext,
        *,
        action: str,
        entity_type: str,
        entity_id: str | uuid.UUID | None = None,
        workflow: ExitWorkflow | None = None,
        changes: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        """Audit a change that does not move the workflow state."""
        self._audit.record(
            ctx,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            workflow=workflow,
            changes=changes,
            context=context,
        )

    @staticmethod
    def is_terminal(state: ExitWorkflowState) -> bool:
        return state in TERMINAL_STATES
