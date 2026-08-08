"""Exit workflow lifecycle: initiation, submission, owner decision, closure.

Covers SRS T13 steps 1-6 and 11, plus the O15 hand-off that turns an owner approval into
an inspection request.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.core.errors import (
    AuthorizationError,
    BusinessRuleViolationError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from app.core.money import quantize
from app.core.pagination import Cursor
from app.domain import events as ev
from app.domain.enums import ActorRole, ExitReason, ExitWorkflowState
from app.domain.events import DomainEvent
from app.domain.policies import (
    NoticeAssessment,
    NoticePolicy,
    assert_documents_complete,
    assess_move_out_date,
    assert_reason_details,
)
from app.domain.state_machine import available_actions
from app.models.exit_workflow import ExitWorkflow
from app.ports.notifications import NotificationTemplate
from app.repositories.exit_workflow import ExitWorkflowRepository
from app.repositories.support import DocumentRepository, InspectionRepository
from app.schemas.exit_workflow import (
    CancelRequest,
    CompleteRequest,
    InitiateExitRequest,
    OwnerApproveRequest,
    OwnerRejectRequest,
    SubmitExitRequest,
    UpdateDraftRequest,
)
from app.services.inspection import InspectionService
from app.services.notifications import (
    EMAIL_AND_PUSH,
    NotificationService,
    base_context,
    owner_recipient,
    tenant_recipient,
)
from app.services.workflow_engine import WorkflowEngine


class ExitWorkflowService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        engine: WorkflowEngine,
        notifications: NotificationService,
        inspections: InspectionService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._engine = engine
        self._notifications = notifications
        self._inspections = inspections
        self._repo = ExitWorkflowRepository(session)
        self._documents = DocumentRepository(session)
        self._inspection_repo = InspectionRepository(session)

    # ------------------------------------------------------------- reads
    async def get(self, workflow_id: uuid.UUID, ctx: RequestContext) -> ExitWorkflow:
        workflow = await self._repo.require(workflow_id)
        agency_id = workflow.inspection.agency_id if workflow.inspection else None
        self._engine.authorise_party(workflow, ctx, agency_id=agency_id)
        return workflow

    async def get_by_reference(self, reference: str, ctx: RequestContext) -> ExitWorkflow:
        workflow = await self._repo.get_by_reference(reference)
        if workflow is None:
            raise NotFoundError(
                "Exit workflow not found.", details={"reference": reference}
            )
        agency_id = workflow.inspection.agency_id if workflow.inspection else None
        self._engine.authorise_party(workflow, ctx, agency_id=agency_id)
        return workflow

    async def list_for_principal(
        self,
        ctx: RequestContext,
        *,
        states: list[ExitWorkflowState] | None = None,
        property_id: uuid.UUID | None = None,
        active_only: bool = False,
        cursor: Cursor | None = None,
        limit: int = 25,
    ) -> tuple[list[ExitWorkflow], bool]:
        """List workflows visible to the caller.

        Scoping is applied here rather than trusted from a query parameter: a tenant sees
        only their own exits, an owner only theirs, an agency only what it was engaged
        for. ADMIN sees everything.
        """
        role = ctx.principal.role
        kwargs: dict[str, Any] = {
            "states": states,
            "property_id": property_id,
            "active_only": active_only,
            "cursor": cursor,
            "limit": limit,
        }
        if role is ActorRole.TENANT:
            kwargs["tenant_id"] = ctx.principal.actor_id
        elif role is ActorRole.OWNER:
            kwargs["owner_id"] = ctx.principal.actor_id
        elif role is ActorRole.INSPECTION_AGENCY:
            kwargs["agency_id"] = ctx.principal.agency_id
        elif role not in (ActorRole.ADMIN, ActorRole.SYSTEM):
            raise AuthorizationError("This role cannot list exit workflows.")
        return await self._repo.list_page(**kwargs)

    def available_actions_for(self, workflow: ExitWorkflow, ctx: RequestContext) -> list[str]:
        return available_actions(workflow.state, ctx.principal.role)

    # ------------------------------------ T13 steps 1-3: initiate a draft
    async def initiate(
        self, request: InitiateExitRequest, ctx: RequestContext
    ) -> ExitWorkflow:
        tenant_id = self._resolve_tenant_id(request, ctx)

        if ctx.principal.role is ActorRole.OWNER and request.owner_id != ctx.principal.actor_id:
            raise AuthorizationError("Owners may only initiate exits for their own properties.")

        move_out_date = request.move_out_date
        notice_days: int | None = None
        notice_waived = False
        if move_out_date is not None and request.reason is not None:
            assessment = self._assess_notice(move_out_date, request.reason, ctx)
            notice_days = assessment.notice_days
            notice_waived = assessment.exempt

        workflow = ExitWorkflow(
            id=uuid.uuid4(),
            reference=None,  # allocated at submission (T13 step 5)
            property_id=request.property_id,
            contract_id=request.contract_id,
            tenant_id=tenant_id,
            owner_id=request.owner_id,
            property_snapshot=request.property_snapshot.model_dump(mode="json"),
            tenant_snapshot=request.tenant_snapshot.model_dump(mode="json"),
            owner_snapshot=request.owner_snapshot.model_dump(mode="json"),
            state=ExitWorkflowState.DRAFT,
            move_out_date=move_out_date,
            reason=request.reason,
            reason_details=assert_reason_details(request.reason, request.reason_details)
            if request.reason
            else None,
            notice_days=notice_days,
            notice_waived=notice_waived,
            currency=request.currency,
            security_deposit_amount=quantize(request.security_deposit_amount),
            initiated_by=ctx.principal.actor_id,
            initiated_by_role=ctx.principal.role,
        )
        self._repo.add(workflow)

        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise self._translate_conflict(exc, request) from exc

        self._engine.audit(
            ctx,
            action="initiate",
            entity_type="exit_workflow",
            entity_id=workflow.id,
            workflow=workflow,
            changes={
                "property_id": str(request.property_id),
                "contract_id": str(request.contract_id),
                "security_deposit_amount": str(workflow.security_deposit_amount),
            },
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.EXIT_INITIATED,
                workflow_id=workflow.id,
                payload={
                    "workflow_id": str(workflow.id),
                    "property_id": str(workflow.property_id),
                    "contract_id": str(workflow.contract_id),
                    "tenant_id": str(workflow.tenant_id),
                    "owner_id": str(workflow.owner_id),
                    "state": workflow.state.value,
                },
            ),
            ctx,
        )
        return workflow

    async def update_draft(
        self, workflow_id: uuid.UUID, request: UpdateDraftRequest, ctx: RequestContext
    ) -> ExitWorkflow:
        workflow = await self._repo.get_for_update(workflow_id)
        self._engine.authorise_party(workflow, ctx, action="update_draft")
        if workflow.state is not ExitWorkflowState.DRAFT:
            raise ConflictError(
                "Only a draft exit request can be edited. Withdraw it first.",
                code="not_a_draft",
                details={"state": workflow.state.value},
            )

        before = {
            "move_out_date": workflow.move_out_date,
            "reason": workflow.reason,
            "reason_details": workflow.reason_details,
        }
        fields = request.model_fields_set
        if "move_out_date" in fields:
            workflow.move_out_date = request.move_out_date
        if "reason" in fields:
            workflow.reason = request.reason
        if "reason_details" in fields:
            workflow.reason_details = request.reason_details

        if workflow.move_out_date is not None and workflow.reason is not None:
            assessment = self._assess_notice(workflow.move_out_date, workflow.reason, ctx)
            workflow.notice_days = assessment.notice_days
            workflow.notice_waived = assessment.exempt
        if workflow.reason is not None:
            workflow.reason_details = assert_reason_details(
                workflow.reason, workflow.reason_details
            )

        after = {
            "move_out_date": workflow.move_out_date,
            "reason": workflow.reason,
            "reason_details": workflow.reason_details,
        }
        self._engine.audit(
            ctx,
            action="update_draft",
            entity_type="exit_workflow",
            entity_id=workflow.id,
            workflow=workflow,
            changes=self._diff(before, after),
        )
        return workflow

    # ------------------------------- T13 steps 5-6: submit, notify owner
    async def submit(
        self, workflow_id: uuid.UUID, request: SubmitExitRequest, ctx: RequestContext
    ) -> ExitWorkflow:
        workflow = await self._repo.get_for_update(workflow_id)
        self._engine.authorise_party(workflow, ctx, action="submit")

        if workflow.move_out_date is None or workflow.reason is None:
            raise ValidationFailedError(
                "A move-out date and a reason must be provided before submitting.",
                details={
                    "missing": [
                        name
                        for name, value in (
                            ("move_out_date", workflow.move_out_date),
                            ("reason", workflow.reason),
                        )
                        if value is None
                    ]
                },
            )

        # Re-validate the date at submission: a draft may have sat long enough for a
        # once-compliant notice period to lapse.
        assessment = self._assess_notice(workflow.move_out_date, workflow.reason, ctx)
        workflow.notice_days = assessment.notice_days
        workflow.notice_waived = assessment.exempt

        documents = await self._documents.list_active(workflow.id)
        assert_documents_complete(
            present={d.document_type for d in documents},
            min_documents=self._settings.min_documents_for_submission,
            required_types=self._required_document_types(),
        )

        if workflow.reference is None:
            workflow.reference = await self._repo.next_reference(self._clock.today().year)

        now = self._clock.now()
        self._engine.transition(
            workflow,
            action="submit",
            ctx=ctx,
            note=request.note,
            event_type=ev.EXIT_SUBMITTED,
            event_payload={
                "move_out_date": workflow.move_out_date.isoformat(),
                "reason": workflow.reason.value,
                "notice_days": workflow.notice_days,
                "document_count": len(documents),
            },
            audit_changes={"reference": str(workflow.reference)},
        )
        workflow.owner_notified_at = now

        self._notifications.enqueue(
            template=NotificationTemplate.OWNER_EXIT_SUBMITTED,
            recipients=(owner_recipient(workflow),),
            context={
                **base_context(workflow),
                "reason": workflow.reason.value,
                "notice_days": workflow.notice_days,
                "approval_sla_days": self._settings.owner_approval_sla_days,
            },
            dedupe_key=f"{workflow.id}:submitted:{workflow.submitted_at}",
        )
        return workflow

    async def withdraw(self, workflow_id: uuid.UUID, ctx: RequestContext) -> ExitWorkflow:
        workflow = await self._repo.get_for_update(workflow_id)
        self._engine.authorise_party(workflow, ctx, action="withdraw")
        self._engine.transition(
            workflow, action="withdraw", ctx=ctx, event_type=ev.EXIT_WITHDRAWN
        )
        return workflow

    # ------------------------------------- O15: owner approval / rejection
    async def owner_approve(
        self, workflow_id: uuid.UUID, request: OwnerApproveRequest, ctx: RequestContext
    ) -> ExitWorkflow:
        """Owner approves the exit, which engages the inspection agency (SRS O15)."""
        workflow = await self._repo.get_for_update(workflow_id)
        self._engine.authorise_party(workflow, ctx, action="owner_approve")

        if request.security_deposit_amount is not None:
            if ctx.principal.role not in (ActorRole.OWNER, ActorRole.ADMIN):
                raise AuthorizationError("Only the owner or an admin may correct the deposit.")
            previous = workflow.security_deposit_amount
            workflow.security_deposit_amount = quantize(request.security_deposit_amount)
            self._engine.audit(
                ctx,
                action="correct_security_deposit",
                entity_type="exit_workflow",
                entity_id=workflow.id,
                workflow=workflow,
                changes={
                    "security_deposit_amount": {
                        "from": str(previous),
                        "to": str(workflow.security_deposit_amount),
                    }
                },
            )

        self._engine.transition(
            workflow,
            action="owner_approve",
            ctx=ctx,
            note=request.note,
            event_type=ev.EXIT_OWNER_APPROVED,
            event_payload={"agency_id": str(request.inspection_agency.agency_id)},
        )

        # O15: "owner approves exit > Workflow ID generated > email sent to registered
        # inspection agency with property details".
        await self._inspections.request_inspection(
            workflow, request.inspection_agency, ctx=ctx
        )

        self._notifications.enqueue(
            template=NotificationTemplate.TENANT_EXIT_APPROVED,
            recipients=(tenant_recipient(workflow),),
            context=base_context(workflow),
            dedupe_key=f"{workflow.id}:approved",
        )
        return workflow

    async def owner_reject(
        self, workflow_id: uuid.UUID, request: OwnerRejectRequest, ctx: RequestContext
    ) -> ExitWorkflow:
        workflow = await self._repo.get_for_update(workflow_id)
        self._engine.authorise_party(workflow, ctx, action="owner_reject")
        self._engine.transition(
            workflow,
            action="owner_reject",
            ctx=ctx,
            note=request.reason,
            event_type=ev.EXIT_OWNER_REJECTED,
            event_payload={"reason": request.reason},
        )
        self._notifications.enqueue(
            template=NotificationTemplate.TENANT_EXIT_REJECTED,
            recipients=(tenant_recipient(workflow),),
            context={**base_context(workflow), "reason": request.reason},
            dedupe_key=f"{workflow.id}:rejected",
        )
        return workflow

    async def cancel(
        self, workflow_id: uuid.UUID, request: CancelRequest, ctx: RequestContext
    ) -> ExitWorkflow:
        workflow = await self._repo.get_for_update(workflow_id)
        self._engine.authorise_party(workflow, ctx, action="cancel")
        self._engine.transition(
            workflow,
            action="cancel",
            ctx=ctx,
            note=request.reason,
            event_type=ev.EXIT_CANCELLED,
            event_payload={"reason": request.reason},
        )
        return workflow

    # -------------------------------------------- T13 step 11: completion
    async def complete(
        self, workflow_id: uuid.UUID, request: CompleteRequest, ctx: RequestContext
    ) -> ExitWorkflow:
        """Close the workflow, releasing the BR-1 lock.

        Requires an issued NOC: completion is what unblocks re-letting the property, so
        it must not precede the certificate that evidences the exit.
        """
        workflow = await self._repo.get_for_update(workflow_id)
        self._engine.authorise_party(workflow, ctx, action="complete")

        if workflow.state is not ExitWorkflowState.NOC_ISSUED:
            raise BusinessRuleViolationError(
                rule="COMPLETION_REQUIRES_NOC",
                message=(
                    "The exit workflow can only be completed once the Exit NOC has been "
                    "issued."
                ),
                details={"state": workflow.state.value},
            )

        self._engine.transition(
            workflow,
            action="complete",
            ctx=ctx,
            note=request.note,
            event_type=ev.WORKFLOW_COMPLETED,
            event_payload={
                "property_released": True,
                "net_refund_amount": str(workflow.net_refund_amount or "0.00"),
            },
        )
        self._notifications.enqueue(
            template=NotificationTemplate.PARTIES_EXIT_COMPLETED,
            recipients=(tenant_recipient(workflow), owner_recipient(workflow)),
            context=base_context(workflow),
            channels=EMAIL_AND_PUSH,
            dedupe_key=f"{workflow.id}:completed",
        )
        return workflow

    async def expire_draft(self, workflow: ExitWorkflow, ctx: RequestContext) -> ExitWorkflow:
        """Used by the reconciler to release abandoned drafts."""
        self._engine.transition(
            workflow,
            action="expire",
            ctx=ctx,
            note=f"Draft abandoned for more than {self._settings.draft_expiry_days} days.",
            event_type=ev.EXIT_EXPIRED,
        )
        return workflow

    # ---------------------------------------------------------- helpers
    def _resolve_tenant_id(
        self, request: InitiateExitRequest, ctx: RequestContext
    ) -> uuid.UUID:
        role = ctx.principal.role
        if role is ActorRole.TENANT:
            if request.tenant_id is not None and request.tenant_id != ctx.principal.actor_id:
                raise AuthorizationError("Tenants may only initiate their own exit.")
            return ctx.principal.actor_id
        if request.tenant_id is None:
            raise ValidationFailedError(
                "tenant_id is required when initiating on a tenant's behalf.",
                details={"field": "tenant_id"},
            )
        if role not in (ActorRole.OWNER, ActorRole.ADMIN):
            raise AuthorizationError("This role cannot initiate an exit workflow.")
        return request.tenant_id

    def _assess_notice(
        self, move_out_date: date, reason: ExitReason, ctx: RequestContext
    ) -> NoticeAssessment:
        return assess_move_out_date(
            move_out_date=move_out_date,
            today=self._clock.today(),
            reason=reason,
            policy=NoticePolicy(
                min_notice_days=self._settings.min_notice_days,
                max_horizon_days=self._settings.max_move_out_horizon_days,
            ),
            admin_override=ctx.principal.role is ActorRole.ADMIN,
        )

    def _required_document_types(self) -> set[Any]:
        from app.domain.enums import DocumentType  # noqa: PLC0415

        required = set()
        for raw in self._settings.required_document_types:
            try:
                required.add(DocumentType(raw))
            except ValueError as exc:
                raise ValidationFailedError(
                    f"Configured required document type {raw!r} is not recognised."
                ) from exc
        return required

    def _translate_conflict(
        self, exc: IntegrityError, request: InitiateExitRequest
    ) -> Exception:
        detail = str(getattr(exc, "orig", exc))
        if "uq_exit_workflow_active_property" in detail:
            return BusinessRuleViolationError(
                rule="SINGLE_ACTIVE_EXIT_PER_PROPERTY",
                message=(
                    "An exit workflow is already in progress for this property. "
                    "Complete or cancel it before starting another."
                ),
                details={"property_id": str(request.property_id)},
            )
        if "uq_exit_workflow_active_contract" in detail:
            return BusinessRuleViolationError(
                rule="SINGLE_ACTIVE_EXIT_PER_CONTRACT",
                message=(
                    "An exit workflow is already in progress for this tenancy contract."
                ),
                details={"contract_id": str(request.contract_id)},
            )
        return ConflictError("The exit workflow could not be created.", details={})

    @staticmethod
    def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        from app.services.audit import AuditService  # noqa: PLC0415

        return AuditService.diff(before, after)
