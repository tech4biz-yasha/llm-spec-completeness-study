"""Shared service plumbing."""

from __future__ import annotations

import uuid
from datetime import date
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.errors import AuthorizationError, NotFoundError
from app.models.workflow import ExitWorkflow
from app.ports.notifications import Notification
from app.ports.outbox import OutboxRecorder
from app.security import PrincipalRole
from app.services.audit import AuditService
from app.services.context import RequestContext


def today_in_market(settings: Settings) -> date:
    """Current calendar day in the market's timezone (Dubai for MVP)."""
    from datetime import datetime

    return datetime.now(ZoneInfo(settings.market_timezone)).date()


class ServiceBase:
    def __init__(
        self,
        session: AsyncSession,
        ctx: RequestContext,
        settings: Settings,
        *,
        recorder: OutboxRecorder | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.settings = settings
        self.events = recorder or OutboxRecorder(
            session, topic_prefix=settings.kafka_topic_prefix
        )
        self.audit = audit or AuditService(
            session, ctx, retention_years=settings.audit_retention_years
        )

    # --- loading ---------------------------------------------------------------------

    async def load_workflow(
        self, workflow_id: uuid.UUID, *, for_update: bool = False
    ) -> ExitWorkflow:
        """Fetch a workflow, optionally taking a row lock for the rest of the transaction.

        Every state-changing operation must pass ``for_update=True``: the lock serialises
        concurrent attempts to advance the same workflow, and the optimistic ``version``
        column catches anything that slips past it.
        """
        stmt = sa.select(ExitWorkflow).where(ExitWorkflow.id == workflow_id)
        if for_update:
            # Relationship loaders run as separate SELECTs, so the lock applies cleanly to
            # the single exit_workflows row.
            stmt = stmt.with_for_update(of=ExitWorkflow)
        workflow = await self.session.scalar(stmt)
        if workflow is None:
            raise NotFoundError(
                "exit workflow not found", details={"workflow_id": str(workflow_id)}
            )
        return workflow

    async def load_workflow_by_reference(self, reference: str) -> ExitWorkflow:
        workflow = await self.session.scalar(
            sa.select(ExitWorkflow).where(ExitWorkflow.reference == reference)
        )
        if workflow is None:
            raise NotFoundError("exit workflow not found", details={"reference": reference})
        return workflow

    # --- authorisation ---------------------------------------------------------------

    def authorize_participant(
        self,
        workflow: ExitWorkflow,
        *,
        allow_tenant: bool = True,
        allow_owner: bool = True,
        allow_agency: bool = False,
    ) -> None:
        """Ensure the caller is a party to *this* workflow, not merely authenticated.

        Admins bypass. Agencies are checked against their live assignment, not the token
        alone, so an agency cannot read a workflow it was never assigned to.
        """
        principal = self.ctx.require_principal()
        if principal.is_admin:
            return

        match principal.role:
            case PrincipalRole.TENANT if allow_tenant and workflow.tenant_id == principal.id:
                return
            case PrincipalRole.OWNER if allow_owner and workflow.owner_id == principal.id:
                return
            case PrincipalRole.AGENCY if allow_agency:
                agency_id = principal.agency_id or principal.id
                if any(a.agency_id == agency_id for a in workflow.assignments):
                    return

        raise AuthorizationError(
            "caller is not a party to this exit workflow",
            details={"workflow_id": str(workflow.id), "role": principal.role.value},
        )

    # --- notifications ----------------------------------------------------------------

    def notify(self, workflow: ExitWorkflow, notification: Notification) -> None:
        """Queue a notification on the outbox, keyed to the workflow's property."""
        self.events.record_notification(
            notification,
            aggregate_id=workflow.id,
            partition_key=str(workflow.property_id),
        )


def workflow_loader_options() -> tuple[object, ...]:
    """Eager-load options for read paths that render a full workflow view."""
    return (
        selectinload(ExitWorkflow.documents),
        selectinload(ExitWorkflow.transitions),
        selectinload(ExitWorkflow.settlement),
        selectinload(ExitWorkflow.noc),
    )
