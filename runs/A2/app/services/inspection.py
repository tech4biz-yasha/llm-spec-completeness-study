"""Third-party inspection workflow (SRS O15) and damage review (T13 step 8, O16)."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.core.errors import (
    AuthorizationError,
    BusinessRuleViolationError,
    ConflictError,
    ValidationFailedError,
)
from app.core.money import quantize, total
from app.domain import events as ev
from app.domain.enums import (
    ActorRole,
    DisputeStatus,
    DocumentType,
    ExitWorkflowState,
    InspectionStatus,
)
from app.domain.events import DomainEvent
from app.domain.policies import assert_dispute_window_open, dispute_window
from app.models.exit_workflow import ExitWorkflow
from app.models.inspection import DamageItem, Inspection, InspectionSlot
from app.ports.notifications import NotificationTemplate, Recipient
from app.repositories.support import DocumentRepository, InspectionRepository
from app.schemas.exit_workflow import AgencyAssignment
from app.schemas.inspection import (
    AdjustDamageItemRequest,
    ProposeSlotsRequest,
    RaiseDisputeRequest,
    RescheduleRequest,
    ResolveDisputeRequest,
    SelectSlotRequest,
    SubmitInspectionReportRequest,
)
from app.services.notifications import (
    EMAIL_ONLY,
    NotificationService,
    base_context,
    owner_recipient,
    tenant_recipient,
)
from app.services.workflow_engine import WorkflowEngine


class InspectionService:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        clock: Clock,
        engine: WorkflowEngine,
        notifications: NotificationService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock
        self._engine = engine
        self._notifications = notifications
        self._repo = InspectionRepository(session)
        self._documents = DocumentRepository(session)

    # --------------------------------------------------------- O15 start
    async def request_inspection(
        self, workflow: ExitWorkflow, agency: AgencyAssignment, *, ctx: RequestContext
    ) -> Inspection:
        """Engage the agency. Called from the owner-approval transition."""
        now = self._clock.now()
        inspection = await self._repo.get_for_workflow(workflow.id)

        if inspection is None:
            inspection = Inspection(
                id=uuid.uuid4(),
                workflow_id=workflow.id,
                agency_id=agency.agency_id,
                agency_name=agency.agency_name,
                agency_email=agency.agency_email,
                status=InspectionStatus.REQUESTED,
                requested_at=now,
                agency_notified_at=now,
                round_number=1,
            )
            self._repo.add(inspection)
        else:
            inspection.agency_id = agency.agency_id
            inspection.agency_name = agency.agency_name
            inspection.agency_email = agency.agency_email
            inspection.status = InspectionStatus.REQUESTED
            inspection.requested_at = now
            inspection.agency_notified_at = now

        await self._session.flush()

        self._engine.audit(
            ctx,
            action="request_inspection",
            entity_type="inspection",
            entity_id=inspection.id,
            workflow=workflow,
            changes={"agency_id": str(agency.agency_id), "agency_name": agency.agency_name},
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.INSPECTION_REQUESTED,
                workflow_id=workflow.id,
                payload={
                    "inspection_id": str(inspection.id),
                    "agency_id": str(agency.agency_id),
                    "agency_email": agency.agency_email,
                    "round_number": inspection.round_number,
                },
            ),
            ctx,
        )

        # O15: "email sent to registered inspection agency with property details".
        self._notifications.enqueue(
            template=NotificationTemplate.AGENCY_INSPECTION_REQUESTED,
            recipients=(
                Recipient(
                    actor_id=str(agency.agency_id),
                    email=agency.agency_email,
                    name=agency.agency_name,
                ),
            ),
            context={
                **base_context(workflow),
                "inspection_id": str(inspection.id),
                "round_number": inspection.round_number,
                "owner_name": (workflow.owner_snapshot or {}).get("name"),
                "tenant_contact": (workflow.tenant_snapshot or {}).get("phone"),
                "property": workflow.property_snapshot,
            },
            channels=EMAIL_ONLY,
            dedupe_key=f"{inspection.id}:requested:{inspection.round_number}",
        )
        return inspection

    # ------------------------------------------------------ O15 scheduling
    async def propose_slots(
        self,
        workflow: ExitWorkflow,
        request: ProposeSlotsRequest,
        *,
        ctx: RequestContext,
    ) -> Inspection:
        """SRS O15: "agency responds with available dates"."""
        inspection = await self._repo.require_for_workflow(workflow.id)
        self._authorise_agency(inspection, ctx)

        now = self._clock.now()
        for slot in request.slots:
            if slot.starts_at <= now:
                raise ValidationFailedError(
                    "Proposed inspection slots must be in the future.",
                    details={"field": "slots", "starts_at": slot.starts_at.isoformat()},
                )

        # Re-proposing replaces the previous offer so the parties never choose a stale slot.
        await self._repo.clear_slot_selection(inspection.id)
        await self._repo.delete_unselected_slots(inspection.id)

        for proposal in request.slots:
            self._session.add(
                InspectionSlot(
                    id=uuid.uuid4(),
                    inspection_id=inspection.id,
                    round_number=inspection.round_number,
                    starts_at=proposal.starts_at,
                    ends_at=proposal.ends_at,
                    note=proposal.note,
                )
            )

        inspection.status = InspectionStatus.SLOTS_PROPOSED
        inspection.slots_proposed_at = now
        inspection.scheduled_start = None
        inspection.scheduled_end = None
        inspection.scheduled_at = None
        inspection.scheduled_by = None
        if request.inspector_name:
            inspection.inspector_name = request.inspector_name
        if request.inspector_licence_no:
            inspection.inspector_licence_no = request.inspector_licence_no

        self._engine.transition(
            workflow,
            action="propose_inspection_slots",
            ctx=ctx,
            note=request.note,
            event_type=ev.INSPECTION_SLOTS_PROPOSED,
            event_payload={
                "inspection_id": str(inspection.id),
                "slot_count": len(request.slots),
            },
        )
        await self._session.flush()
        await self._session.refresh(inspection, ["slots"])

        self._notifications.enqueue(
            template=NotificationTemplate.PARTIES_INSPECTION_SLOTS_AVAILABLE,
            recipients=(tenant_recipient(workflow), owner_recipient(workflow)),
            context={
                **base_context(workflow),
                "agency_name": inspection.agency_name,
                "slots": [
                    {
                        "id": str(s.id),
                        "starts_at": s.starts_at.isoformat(),
                        "ends_at": s.ends_at.isoformat(),
                    }
                    for s in inspection.slots
                ],
            },
            dedupe_key=f"{inspection.id}:slots:{now.isoformat()}",
        )
        return inspection

    async def select_slot(
        self, workflow: ExitWorkflow, request: SelectSlotRequest, *, ctx: RequestContext
    ) -> Inspection:
        """SRS T13 step 7 / O15: "owner/tenant select date"."""
        inspection = await self._repo.require_for_workflow(workflow.id)
        self._engine.authorise_party(workflow, ctx, action="select_inspection_slot")

        slot = await self._repo.require_slot(inspection.id, request.slot_id)
        if slot.starts_at <= self._clock.now():
            raise ValidationFailedError(
                "That inspection slot is in the past. Ask the agency for new dates.",
                details={"slot_id": str(slot.id)},
            )

        now = self._clock.now()
        await self._repo.clear_slot_selection(inspection.id)
        slot.is_selected = True
        slot.selected_at = now
        slot.selected_by = ctx.principal.actor_id

        inspection.status = InspectionStatus.SCHEDULED
        inspection.scheduled_start = slot.starts_at
        inspection.scheduled_end = slot.ends_at
        inspection.scheduled_at = now
        inspection.scheduled_by = ctx.principal.actor_id

        self._engine.transition(
            workflow,
            action="select_inspection_slot",
            ctx=ctx,
            event_type=ev.INSPECTION_SCHEDULED,
            event_payload={
                "inspection_id": str(inspection.id),
                "slot_id": str(slot.id),
                "scheduled_start": slot.starts_at.isoformat(),
                "scheduled_end": slot.ends_at.isoformat(),
            },
        )
        self._notifications.enqueue(
            template=NotificationTemplate.PARTIES_INSPECTION_SCHEDULED,
            recipients=(
                tenant_recipient(workflow),
                owner_recipient(workflow),
                Recipient(
                    actor_id=str(inspection.agency_id),
                    email=inspection.agency_email,
                    name=inspection.agency_name,
                ),
            ),
            context={
                **base_context(workflow),
                "scheduled_start": slot.starts_at.isoformat(),
                "scheduled_end": slot.ends_at.isoformat(),
                "agency_name": inspection.agency_name,
            },
            dedupe_key=f"{inspection.id}:scheduled:{slot.id}",
        )
        return inspection

    async def reschedule(
        self, workflow: ExitWorkflow, request: RescheduleRequest, *, ctx: RequestContext
    ) -> Inspection:
        inspection = await self._repo.require_for_workflow(workflow.id)
        if ctx.principal.role is ActorRole.INSPECTION_AGENCY:
            self._authorise_agency(inspection, ctx)
        else:
            self._engine.authorise_party(workflow, ctx, action="reschedule_inspection")

        await self._repo.clear_slot_selection(inspection.id)
        inspection.status = InspectionStatus.SLOTS_PROPOSED
        inspection.scheduled_start = None
        inspection.scheduled_end = None
        inspection.scheduled_at = None
        inspection.scheduled_by = None

        self._engine.transition(
            workflow,
            action="reschedule_inspection",
            ctx=ctx,
            note=request.reason,
            event_type=ev.INSPECTION_RESCHEDULED,
            event_payload={"inspection_id": str(inspection.id), "reason": request.reason},
        )
        return inspection

    # --------------------------------------------------- O16 damage report
    async def submit_report(
        self,
        workflow: ExitWorkflow,
        request: SubmitInspectionReportRequest,
        *,
        ctx: RequestContext,
    ) -> Inspection:
        """SRS O16: the agency uploads the damage report with photos."""
        inspection = await self._repo.require_for_workflow(workflow.id)
        self._authorise_agency(inspection, ctx)

        now = self._clock.now()
        if request.conducted_at > now:
            raise ValidationFailedError(
                "conducted_at cannot be in the future.",
                details={"field": "conducted_at"},
            )

        if request.report_document_id is not None:
            report = await self._documents.require_in_workflow(
                request.report_document_id, workflow.id
            )
            if report.document_type is not DocumentType.INSPECTION_REPORT:
                raise ValidationFailedError(
                    "report_document_id must reference an INSPECTION_REPORT document.",
                    details={"document_id": str(report.id)},
                )
            inspection.report_document_id = report.id

        await self._validate_photo_ids(request, workflow.id)

        # A resubmission for the same round replaces the previous assessment wholesale;
        # superseded items would otherwise double-count in the deduction total.
        for existing in list(inspection.damage_items):
            if existing.round_number == inspection.round_number:
                inspection.damage_items.remove(existing)

        items: list[DamageItem] = []
        for entry in request.damage_items:
            item = DamageItem(
                id=uuid.uuid4(),
                inspection_id=inspection.id,
                round_number=inspection.round_number,
                category=entry.category,
                severity=entry.severity,
                location=entry.location,
                description=entry.description,
                estimated_cost=quantize(entry.estimated_cost),
                tenant_liable=entry.tenant_liable,
                photo_document_ids=[str(pid) for pid in entry.photo_document_ids],
            )
            items.append(item)
            self._session.add(item)

        inspection.status = InspectionStatus.COMPLETED
        inspection.conducted_at = request.conducted_at
        inspection.reported_at = now
        inspection.overall_condition = request.overall_condition
        inspection.report_summary = request.report_summary
        if request.inspector_name:
            inspection.inspector_name = request.inspector_name
        inspection.assessed_total = total(i.estimated_cost for i in items)

        self._engine.transition(
            workflow,
            action="submit_inspection_report",
            ctx=ctx,
            event_type=ev.INSPECTION_REPORT_SUBMITTED,
            event_payload={
                "inspection_id": str(inspection.id),
                "damage_item_count": len(items),
                "assessed_total": str(inspection.assessed_total),
                "overall_condition": request.overall_condition.value,
            },
        )
        self._notifications.enqueue(
            template=NotificationTemplate.PARTIES_INSPECTION_REPORT_READY,
            recipients=(tenant_recipient(workflow), owner_recipient(workflow)),
            context={
                **base_context(workflow),
                "assessed_total": str(inspection.assessed_total),
                "damage_item_count": len(items),
            },
            dedupe_key=f"{inspection.id}:report:{inspection.round_number}",
        )

        # T13 step 8 follows immediately; nobody has to press a button to start reviewing.
        await self.open_damage_review(workflow, ctx=ctx.as_system())
        return inspection

    async def open_damage_review(
        self, workflow: ExitWorkflow, *, ctx: RequestContext
    ) -> ExitWorkflow:
        now = self._clock.now()
        window = dispute_window(now, self._settings.dispute_window_days)
        workflow.damage_review_opened_at = window.opened_at
        workflow.dispute_window_closes_at = window.closes_at

        self._engine.transition(
            workflow,
            action="open_damage_review",
            ctx=ctx,
            event_type=ev.DAMAGE_REVIEW_OPENED,
            event_payload={
                "dispute_window_closes_at": window.closes_at.isoformat(),
                "dispute_window_days": self._settings.dispute_window_days,
            },
        )
        self._notifications.enqueue(
            template=NotificationTemplate.TENANT_DAMAGE_REVIEW_OPENED,
            recipients=(tenant_recipient(workflow),),
            context={
                **base_context(workflow),
                "dispute_window_closes_at": window.closes_at.isoformat(),
            },
            dedupe_key=f"{workflow.id}:damage_review:{now.isoformat()}",
        )
        return workflow

    # ------------------------------------------- T13 step 8: damage review
    async def adjust_damage_item(
        self,
        workflow: ExitWorkflow,
        item_id: uuid.UUID,
        request: AdjustDamageItemRequest,
        *,
        ctx: RequestContext,
    ) -> DamageItem:
        """Owner accepts, reduces or waives an assessed charge."""
        self._require_state(workflow, ExitWorkflowState.DAMAGE_REVIEW, "adjust a damage item")
        if ctx.principal.role not in (ActorRole.OWNER, ActorRole.ADMIN):
            raise AuthorizationError("Only the owner may adjust assessed damages.")
        self._engine.authorise_party(workflow, ctx, action="adjust_damage_item")

        inspection = await self._repo.require_for_workflow(workflow.id)
        item = await self._repo.require_damage_item(inspection.id, item_id)

        before = {
            "approved_cost": item.approved_cost,
            "tenant_liable": item.tenant_liable,
        }
        if request.approved_cost is not None:
            approved = quantize(request.approved_cost)
            if approved > item.estimated_cost:
                raise BusinessRuleViolationError(
                    rule="APPROVED_COST_ABOVE_ASSESSMENT",
                    message=(
                        "The approved charge cannot exceed the amount assessed by the "
                        "inspection agency. Request a re-inspection instead."
                    ),
                    details={
                        "estimated_cost": str(item.estimated_cost),
                        "approved_cost": str(approved),
                    },
                )
            item.approved_cost = approved
        if request.tenant_liable is not None:
            item.tenant_liable = request.tenant_liable

        item.adjusted_by = ctx.principal.actor_id
        item.adjusted_at = self._clock.now()
        item.adjustment_note = request.note

        self._engine.audit(
            ctx,
            action="adjust_damage_item",
            entity_type="damage_item",
            entity_id=item.id,
            workflow=workflow,
            changes={
                "approved_cost": {
                    "from": str(before["approved_cost"]),
                    "to": str(item.approved_cost),
                },
                "tenant_liable": {
                    "from": before["tenant_liable"],
                    "to": item.tenant_liable,
                },
            },
            context={"note": request.note} if request.note else None,
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.DAMAGE_ITEM_ADJUSTED,
                workflow_id=workflow.id,
                payload={
                    "damage_item_id": str(item.id),
                    "approved_cost": str(item.approved_cost),
                    "chargeable_amount": str(item.chargeable_amount),
                },
            ),
            ctx,
        )
        return item

    async def raise_dispute(
        self,
        workflow: ExitWorkflow,
        item_id: uuid.UUID,
        request: RaiseDisputeRequest,
        *,
        ctx: RequestContext,
    ) -> DamageItem:
        """Tenant objects to an assessed charge within the dispute window."""
        self._require_state(workflow, ExitWorkflowState.DAMAGE_REVIEW, "dispute a damage item")
        if ctx.principal.role not in (ActorRole.TENANT, ActorRole.ADMIN):
            raise AuthorizationError("Only the tenant may dispute an assessed damage.")
        self._engine.authorise_party(workflow, ctx, action="raise_dispute")

        if workflow.damage_review_opened_at is None or workflow.dispute_window_closes_at is None:
            raise ConflictError("The damage review window has not been opened yet.")
        assert_dispute_window_open(
            dispute_window(
                workflow.damage_review_opened_at, self._settings.dispute_window_days
            ),
            self._clock.now(),
        )

        inspection = await self._repo.require_for_workflow(workflow.id)
        item = await self._repo.require_damage_item(inspection.id, item_id)
        if item.dispute_status is not DisputeStatus.NONE:
            raise ConflictError(
                "This damage item has already been disputed.",
                code="dispute_already_raised",
                details={"dispute_status": item.dispute_status.value},
            )

        item.dispute_status = DisputeStatus.RAISED
        item.dispute_reason = request.reason
        item.disputed_at = self._clock.now()
        item.disputed_by = ctx.principal.actor_id

        self._engine.audit(
            ctx,
            action="raise_dispute",
            entity_type="damage_item",
            entity_id=item.id,
            workflow=workflow,
            changes={"dispute_status": {"from": "NONE", "to": "RAISED"}},
            context={"reason": request.reason},
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.DAMAGE_DISPUTE_RAISED,
                workflow_id=workflow.id,
                payload={
                    "damage_item_id": str(item.id),
                    "reason": request.reason,
                    "amount": str(item.chargeable_amount),
                },
            ),
            ctx,
        )
        self._notifications.enqueue(
            template=NotificationTemplate.OWNER_DISPUTE_RAISED,
            recipients=(owner_recipient(workflow),),
            context={
                **base_context(workflow),
                "damage_item_id": str(item.id),
                "description": item.description,
                "reason": request.reason,
            },
            dedupe_key=f"{item.id}:disputed",
        )
        return item

    async def resolve_dispute(
        self,
        workflow: ExitWorkflow,
        item_id: uuid.UUID,
        request: ResolveDisputeRequest,
        *,
        ctx: RequestContext,
    ) -> DamageItem:
        self._require_state(workflow, ExitWorkflowState.DAMAGE_REVIEW, "resolve a dispute")
        if ctx.principal.role not in (ActorRole.OWNER, ActorRole.ADMIN):
            raise AuthorizationError("Only the owner may resolve a dispute.")
        self._engine.authorise_party(workflow, ctx, action="resolve_dispute")

        inspection = await self._repo.require_for_workflow(workflow.id)
        item = await self._repo.require_damage_item(inspection.id, item_id)
        if item.dispute_status is not DisputeStatus.RAISED:
            raise ConflictError(
                "There is no open dispute on this damage item.",
                code="no_open_dispute",
                details={"dispute_status": item.dispute_status.value},
            )

        if request.uphold:
            approved = quantize(request.approved_cost or Decimal("0.00"))
            if approved > item.estimated_cost:
                raise ValidationFailedError(
                    "The revised charge cannot exceed the amount originally assessed.",
                    details={"estimated_cost": str(item.estimated_cost)},
                )
            item.approved_cost = approved
            item.dispute_status = DisputeStatus.UPHELD
        else:
            item.dispute_status = DisputeStatus.REJECTED

        item.dispute_resolution_note = request.note
        item.dispute_resolved_at = self._clock.now()
        item.dispute_resolved_by = ctx.principal.actor_id

        self._engine.audit(
            ctx,
            action="resolve_dispute",
            entity_type="damage_item",
            entity_id=item.id,
            workflow=workflow,
            changes={
                "dispute_status": {"from": "RAISED", "to": item.dispute_status.value},
                "approved_cost": str(item.approved_cost),
            },
            context={"note": request.note} if request.note else None,
        )
        self._engine.record_event(
            DomainEvent(
                event_type=ev.DAMAGE_DISPUTE_RESOLVED,
                workflow_id=workflow.id,
                payload={
                    "damage_item_id": str(item.id),
                    "outcome": item.dispute_status.value,
                    "chargeable_amount": str(item.chargeable_amount),
                },
            ),
            ctx,
        )
        self._notifications.enqueue(
            template=NotificationTemplate.TENANT_DISPUTE_RESOLVED,
            recipients=(tenant_recipient(workflow),),
            context={
                **base_context(workflow),
                "damage_item_id": str(item.id),
                "outcome": item.dispute_status.value,
                "chargeable_amount": str(item.chargeable_amount),
            },
            dedupe_key=f"{item.id}:dispute_resolved",
        )
        return item

    async def request_reinspection(
        self, workflow: ExitWorkflow, request: RescheduleRequest, *, ctx: RequestContext
    ) -> Inspection:
        """Escalate a contested assessment to a fresh inspection round."""
        if ctx.principal.role not in (ActorRole.OWNER, ActorRole.ADMIN):
            raise AuthorizationError("Only the owner may request a re-inspection.")
        self._engine.authorise_party(workflow, ctx, action="request_reinspection")

        inspection = await self._repo.require_for_workflow(workflow.id)
        inspection.round_number += 1
        inspection.status = InspectionStatus.REQUESTED
        inspection.requested_at = self._clock.now()
        inspection.agency_notified_at = self._clock.now()
        inspection.conducted_at = None
        inspection.reported_at = None
        inspection.scheduled_start = None
        inspection.scheduled_end = None
        await self._repo.clear_slot_selection(inspection.id)

        workflow.damage_review_opened_at = None
        workflow.dispute_window_closes_at = None

        self._engine.transition(
            workflow,
            action="request_reinspection",
            ctx=ctx,
            note=request.reason,
            event_type=ev.INSPECTION_REQUESTED,
            event_payload={
                "inspection_id": str(inspection.id),
                "round_number": inspection.round_number,
                "reason": request.reason,
            },
        )
        self._notifications.enqueue(
            template=NotificationTemplate.AGENCY_INSPECTION_REQUESTED,
            recipients=(
                Recipient(
                    actor_id=str(inspection.agency_id),
                    email=inspection.agency_email,
                    name=inspection.agency_name,
                ),
            ),
            context={
                **base_context(workflow),
                "inspection_id": str(inspection.id),
                "round_number": inspection.round_number,
                "reason": request.reason,
                "property": workflow.property_snapshot,
            },
            channels=EMAIL_ONLY,
            dedupe_key=f"{inspection.id}:requested:{inspection.round_number}",
        )
        return inspection

    # ---------------------------------------------------------- helpers
    async def get_for_workflow(self, workflow: ExitWorkflow) -> Inspection:
        return await self._repo.require_for_workflow(workflow.id)

    async def count_open_disputes(self, inspection_id: uuid.UUID) -> int:
        return await self._repo.count_open_disputes(inspection_id)

    def _authorise_agency(self, inspection: Inspection, ctx: RequestContext) -> None:
        role = ctx.principal.role
        if role in (ActorRole.ADMIN, ActorRole.SYSTEM):
            return
        if role is not ActorRole.INSPECTION_AGENCY:
            raise AuthorizationError("Only the engaged inspection agency may do this.")
        if ctx.principal.agency_id != inspection.agency_id:
            raise AuthorizationError(
                "Your agency is not the one engaged for this inspection.",
                details={"inspection_id": str(inspection.id)},
            )

    @staticmethod
    def _require_state(
        workflow: ExitWorkflow, state: ExitWorkflowState, action: str
    ) -> None:
        if workflow.state is not state:
            raise ConflictError(
                f"The exit workflow must be in {state.value} to {action}; "
                f"it is currently {workflow.state.value}.",
                code="illegal_state_for_action",
                details={"required_state": state.value, "current_state": workflow.state.value},
            )

    async def _validate_photo_ids(
        self, request: SubmitInspectionReportRequest, workflow_id: uuid.UUID
    ) -> None:
        """Every referenced photo must be a DAMAGE_PHOTO already attached to this workflow."""
        referenced = {pid for entry in request.damage_items for pid in entry.photo_document_ids}
        if not referenced:
            return
        available = {
            d.id: d
            for d in await self._documents.list_active(workflow_id)
            if d.document_type is DocumentType.DAMAGE_PHOTO
        }
        unknown = sorted(str(pid) for pid in referenced - set(available))
        if unknown:
            raise ValidationFailedError(
                "Some referenced damage photos are not attached to this exit workflow.",
                details={"unknown_photo_document_ids": unknown},
            )
