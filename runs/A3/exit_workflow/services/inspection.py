"""O15 — third-party inspection workflow, and the damage report it yields.

Appendix B flow:
    owner approves exit > Workflow ID generated > email sent to registered
    inspection agency with property details > agency responds with available
    dates > owner/tenant select date > inspection occurs > report uploaded
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.errors import ConflictError, NotFoundError, ValidationError
from exit_workflow.core.ids import request_reference
from exit_workflow.core.money import ensure_non_negative
from exit_workflow.core.security import Role
from exit_workflow.domain import policy
from exit_workflow.domain.enums import (
    DamageCategory,
    DamageReportStatus,
    DamageSeverity,
    ExitWorkflowStatus,
    InspectionStatus,
    SlotStatus,
    TenantReviewDecision,
)
from exit_workflow.integrations.agencies import AgencyDirectory
from exit_workflow.models.inspection import (
    DamageLineItem,
    DamageReport,
    Inspection,
    InspectionSlot,
)
from exit_workflow.models.workflow import ExitWorkflow
from exit_workflow.services import access
from exit_workflow.services.audit import AuditRecorder
from exit_workflow.services.context import ServiceContext
from exit_workflow.services.documents import DocumentService
from exit_workflow.services.events import AggregateType, EventRecorder, EventType
from exit_workflow.services.notifications import NotificationService, Template
from exit_workflow.services.transitions import apply_transition

MAX_SLOTS_PER_PROPOSAL = 10
MAX_LINE_ITEMS = 200


@dataclass(frozen=True, slots=True)
class SlotInput:
    starts_at: datetime
    ends_at: datetime
    note: str | None = None


@dataclass(frozen=True, slots=True)
class LineItemInput:
    category: DamageCategory
    severity: DamageSeverity
    description: str
    assessed_amount: Decimal
    location: str | None = None
    tenant_liable: bool = True
    notes: str | None = None
    photo_document_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DamageReportInput:
    inspected_at: datetime
    line_items: list[LineItemInput]
    summary: str | None = None
    inspector_name: str | None = None


class InspectionService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        ctx: ServiceContext,
        *,
        audit: AuditRecorder,
        events: EventRecorder,
        notifications: NotificationService,
        agencies: AgencyDirectory,
        documents: DocumentService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._ctx = ctx
        self._audit = audit
        self._events = events
        self._notifications = notifications
        self._agencies = agencies
        self._documents = documents

    # -- loading -----------------------------------------------------------
    async def load(
        self, inspection_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[Inspection, ExitWorkflow]:
        """Load an inspection together with its workflow.

        The workflow row is locked *first* and always, so concurrent actions on
        one exit serialise in a consistent order and cannot deadlock.
        """

        inspection = await self._session.get(Inspection, inspection_id)
        if inspection is None:
            raise NotFoundError("Inspection not found.")

        stmt = select(ExitWorkflow).where(ExitWorkflow.id == inspection.workflow_id)
        if for_update:
            stmt = stmt.with_for_update(of=ExitWorkflow)
        workflow = (await self._session.execute(stmt)).scalars().one()
        if for_update:
            await self._session.refresh(inspection)

        principal = self._ctx.require_principal()
        if principal.role is Role.INSPECTION_AGENCY:
            access.ensure_is_assigned_agency(inspection, principal)
        else:
            access.ensure_can_view(workflow, principal)
        return inspection, workflow

    async def active_inspection(self, workflow: ExitWorkflow) -> Inspection | None:
        stmt = select(Inspection).where(
            Inspection.workflow_id == workflow.id,
            Inspection.status.notin_(
                [InspectionStatus.COMPLETED, InspectionStatus.CANCELLED]
            ),
        )
        return (await self._session.execute(stmt)).scalars().first()

    # -- request (workflow: OWNER_APPROVED -> INSPECTION_REQUESTED) --------
    async def request_inspection(
        self, workflow: ExitWorkflow, *, agency_id: uuid.UUID, notes: str | None = None
    ) -> Inspection:
        access.ensure_is_owner(workflow, self._ctx.require_principal())

        agency = await self._agencies.get_agency(agency_id)
        agency.ensure_engageable()

        if await self.active_inspection(workflow) is not None:
            raise ConflictError(
                "An inspection is already in progress for this exit workflow; cancel it "
                "before requesting another."
            )

        next_attempt = int(
            (
                await self._session.execute(
                    select(func.coalesce(func.max(Inspection.attempt_no), 0)).where(
                        Inspection.workflow_id == workflow.id
                    )
                )
            ).scalar_one()
            or 0
        ) + 1

        now = utcnow()
        inspection = Inspection(
            workflow=workflow,
            attempt_no=next_attempt,
            reference=request_reference(),
            agency_id=agency.agency_id,
            agency_name=agency.name,
            agency_email=agency.email,
            status=InspectionStatus.REQUESTED,
            request_notes=notes,
            requested_at=now,
            requested_by=self._ctx.require_principal().subject_id,
        )
        self._session.add(inspection)

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.INSPECTION_REQUESTED,
            reason=f"Inspection requested from {agency.name}",
            attributes={"inspection_reference": inspection.reference},
        )
        workflow.inspection_requested_at = now

        self._audit.record(
            self._ctx,
            action="inspection.requested",
            entity_type="inspection",
            entity_id=inspection.id,
            workflow_id=workflow.id,
            changes={"agency_id": agency.agency_id, "agency_name": agency.name, "notes": notes},
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.INSPECTION_REQUESTED,
            aggregate_type=AggregateType.INSPECTION,
            aggregate_id=inspection.id,
            workflow_id=workflow.id,
            payload={
                "inspection_id": inspection.id,
                "inspection_reference": inspection.reference,
                "agency_id": agency.agency_id,
                "attempt_no": next_attempt,
                "property_id": workflow.property_id,
            },
        )
        # Appendix B: the agency email carries the property details + workflow id.
        self._notifications.enqueue(
            template=Template.AGENCY_INSPECTION_REQUESTED,
            recipient=agency.email,
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "inspection_reference": inspection.reference,
                "agency_name": agency.name,
                "property_address": workflow.property_address,
                "property_reference": workflow.property_reference,
                "move_out_date": workflow.move_out_date,
                "tenant_name": workflow.tenant_name,
                "owner_name": workflow.owner_name,
                "request_notes": notes,
            },
        )
        await self._session.flush()
        return inspection

    # -- agency proposes availability --------------------------------------
    async def propose_slots(
        self, inspection: Inspection, workflow: ExitWorkflow, slots: list[SlotInput]
    ) -> list[InspectionSlot]:
        access.ensure_is_assigned_agency(inspection, self._ctx.require_principal())

        if inspection.status not in (
            InspectionStatus.REQUESTED,
            InspectionStatus.SLOTS_PROPOSED,
            InspectionStatus.SCHEDULED,
        ):
            raise ConflictError(
                f"Inspection is {inspection.status.value}; availability can no longer be "
                "proposed."
            )
        if not slots:
            raise ValidationError("At least one availability slot is required.")
        if len(slots) > MAX_SLOTS_PER_PROPOSAL:
            raise ValidationError(
                f"At most {MAX_SLOTS_PER_PROPOSAL} slots may be proposed at once."
            )

        now = utcnow()
        for slot in slots:
            policy.validate_slot(slot.starts_at, slot.ends_at, now, self._settings)

        # Re-proposing supersedes anything still on the table, including a
        # previously confirmed appointment.
        was_scheduled = inspection.status is InspectionStatus.SCHEDULED
        for existing in inspection.slots:
            if existing.status in (SlotStatus.PROPOSED, SlotStatus.SELECTED):
                existing.status = SlotStatus.DECLINED

        created = [
            InspectionSlot(
                # Bound through the relationship so the in-session aggregate
                # (and the response built from it) sees the new slots.
                inspection=inspection,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                status=SlotStatus.PROPOSED,
                proposed_at=now,
                note=slot.note,
            )
            for slot in slots
        ]
        self._session.add_all(created)

        inspection.status = InspectionStatus.SLOTS_PROPOSED
        inspection.scheduled_slot_id = None
        inspection.scheduled_start = None
        inspection.scheduled_end = None
        inspection.scheduled_at = None
        inspection.scheduled_by = None

        if was_scheduled and workflow.status is ExitWorkflowStatus.INSPECTION_SCHEDULED:
            apply_transition(
                self._session,
                self._ctx,
                self._audit,
                self._events,
                workflow,
                ExitWorkflowStatus.INSPECTION_REQUESTED,
                reason="Agency released the appointment and proposed new dates",
            )
            workflow.inspection_scheduled_at = None

        self._audit.record(
            self._ctx,
            action="inspection.slots_proposed",
            entity_type="inspection",
            entity_id=inspection.id,
            workflow_id=workflow.id,
            changes={"slot_count": len(created)},
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.INSPECTION_SLOTS_PROPOSED,
            aggregate_type=AggregateType.INSPECTION,
            aggregate_id=inspection.id,
            workflow_id=workflow.id,
            payload={
                "inspection_id": inspection.id,
                "slots": [
                    {"id": s.id, "starts_at": s.starts_at, "ends_at": s.ends_at} for s in created
                ],
            },
        )
        self._notifications.enqueue_many(
            template=Template.PARTIES_SLOTS_PROPOSED,
            recipients=[workflow.owner_email, workflow.tenant_email],
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "agency_name": inspection.agency_name,
                "property_address": workflow.property_address,
                "slots": [
                    f"{s.starts_at.isoformat()} – {s.ends_at.isoformat()}" for s in created
                ],
            },
        )
        await self._session.flush()
        return created

    # -- owner/tenant selects a date ---------------------------------------
    async def schedule(
        self, inspection: Inspection, workflow: ExitWorkflow, *, slot_id: uuid.UUID
    ) -> Inspection:
        access.ensure_is_party(workflow, self._ctx.require_principal())

        if inspection.status is not InspectionStatus.SLOTS_PROPOSED:
            raise ConflictError(
                f"Inspection is {inspection.status.value}; no dates are awaiting selection."
            )

        slot = next((s for s in inspection.slots if s.id == slot_id), None)
        if slot is None:
            raise NotFoundError("Proposed inspection slot not found.")
        if slot.status is not SlotStatus.PROPOSED:
            raise ConflictError(f"Slot is {slot.status.value} and cannot be selected.")
        if slot.starts_at <= utcnow():
            raise ValidationError("That slot has already started; ask the agency for new dates.")

        now = utcnow()
        for other in inspection.slots:
            other.status = SlotStatus.SELECTED if other.id == slot.id else SlotStatus.DECLINED

        inspection.status = InspectionStatus.SCHEDULED
        inspection.scheduled_slot_id = slot.id
        inspection.scheduled_start = slot.starts_at
        inspection.scheduled_end = slot.ends_at
        inspection.scheduled_at = now
        inspection.scheduled_by = self._ctx.actor_id

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.INSPECTION_SCHEDULED,
            reason="Inspection appointment confirmed",
            attributes={"scheduled_start": slot.starts_at.isoformat()},
        )
        workflow.inspection_scheduled_at = now

        self._audit.record(
            self._ctx,
            action="inspection.scheduled",
            entity_type="inspection",
            entity_id=inspection.id,
            workflow_id=workflow.id,
            changes={"slot_id": slot.id, "starts_at": slot.starts_at, "ends_at": slot.ends_at},
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.INSPECTION_SCHEDULED,
            aggregate_type=AggregateType.INSPECTION,
            aggregate_id=inspection.id,
            workflow_id=workflow.id,
            payload={
                "inspection_id": inspection.id,
                "scheduled_start": slot.starts_at,
                "scheduled_end": slot.ends_at,
                "agency_id": inspection.agency_id,
            },
        )
        self._notifications.enqueue_many(
            template=Template.PARTIES_INSPECTION_SCHEDULED,
            recipients=[workflow.owner_email, workflow.tenant_email, inspection.agency_email],
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "property_address": workflow.property_address,
                "scheduled_start": slot.starts_at,
                "scheduled_end": slot.ends_at,
                "agency_name": inspection.agency_name,
            },
        )
        await self._session.flush()
        return inspection

    async def cancel(
        self, inspection: Inspection, workflow: ExitWorkflow, *, reason: str
    ) -> Inspection:
        principal = self._ctx.require_principal()
        if principal.role is Role.INSPECTION_AGENCY:
            access.ensure_is_assigned_agency(inspection, principal)
        elif not principal.is_admin:
            access.ensure_is_owner(workflow, principal)

        if not inspection.is_active:
            raise ConflictError(f"Inspection is already {inspection.status.value}.")
        if not reason.strip():
            raise ValidationError("A cancellation reason is required.", extra={"field": "reason"})

        now = utcnow()
        inspection.status = InspectionStatus.CANCELLED
        inspection.cancelled_at = now
        inspection.cancellation_reason = reason
        for slot in inspection.slots:
            if slot.status in (SlotStatus.PROPOSED, SlotStatus.SELECTED):
                slot.status = SlotStatus.DECLINED

        # The exit returns to "approved, awaiting inspection" so another agency
        # can be engaged without restarting the whole workflow.
        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.OWNER_APPROVED,
            reason=f"Inspection cancelled: {reason}",
        )
        workflow.inspection_requested_at = None
        workflow.inspection_scheduled_at = None

        self._audit.record(
            self._ctx,
            action="inspection.cancelled",
            entity_type="inspection",
            entity_id=inspection.id,
            workflow_id=workflow.id,
            changes={"reason": reason},
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.INSPECTION_CANCELLED,
            aggregate_type=AggregateType.INSPECTION,
            aggregate_id=inspection.id,
            workflow_id=workflow.id,
            payload={"inspection_id": inspection.id, "reason": reason},
        )
        await self._session.flush()
        return inspection

    # -- report upload (O16 input) -----------------------------------------
    async def submit_damage_report(
        self, inspection: Inspection, workflow: ExitWorkflow, data: DamageReportInput
    ) -> DamageReport:
        access.ensure_is_assigned_agency(inspection, self._ctx.require_principal())

        if inspection.status is not InspectionStatus.SCHEDULED:
            raise ConflictError(
                f"Inspection is {inspection.status.value}; a report can only be uploaded for a "
                "scheduled inspection."
            )
        if inspection.damage_report is not None:  # pragma: no cover - unique constraint
            raise ConflictError("A damage report has already been uploaded for this inspection.")

        now = utcnow()
        if data.inspected_at > now:
            raise ValidationError("inspected_at cannot be in the future.")
        if len(data.line_items) > MAX_LINE_ITEMS:
            raise ValidationError(f"A report may contain at most {MAX_LINE_ITEMS} line items.")

        report = DamageReport(
            inspection=inspection,
            workflow_id=workflow.id,
            status=DamageReportStatus.SUBMITTED,
            summary=data.summary,
            inspected_at=data.inspected_at,
            submitted_at=now,
            submitted_by=self._ctx.require_principal().subject_id,
            inspector_name=data.inspector_name,
            currency=workflow.currency,
            assessed_total=0,
        )
        self._session.add(report)

        total = Decimal("0.00")
        for item in data.line_items:
            amount = ensure_non_negative(item.assessed_amount, "assessed_amount")
            if not item.description.strip():
                raise ValidationError("Every damage line item needs a description.")
            line = DamageLineItem(
                report=report,
                category=item.category,
                severity=item.severity,
                description=item.description.strip(),
                location=item.location,
                assessed_amount=amount,
                tenant_liable=item.tenant_liable,
                notes=item.notes,
            )
            self._session.add(line)
            await self._documents.attach_photos_to_line_item(
                workflow, line, item.photo_document_ids
            )
            if item.tenant_liable:
                total += amount

        report.assessed_total = total

        inspection.status = InspectionStatus.COMPLETED
        inspection.conducted_at = data.inspected_at
        inspection.completed_at = now

        # Two recorded transitions: the inspection finished, and the exit moved
        # into damage review (T13 step 7).
        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.INSPECTION_COMPLETED,
            reason="Inspection conducted and report uploaded",
        )
        workflow.inspection_completed_at = now
        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.DAMAGE_REVIEW,
            reason="Damage report available for review",
            system=True,
        )

        self._audit.record(
            self._ctx,
            action="damage_report.submitted",
            entity_type="damage_report",
            entity_id=report.id,
            workflow_id=workflow.id,
            changes={
                "assessed_total": total,
                "line_item_count": len(data.line_items),
                "inspected_at": data.inspected_at,
            },
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.DAMAGE_REPORT_SUBMITTED,
            aggregate_type=AggregateType.DAMAGE_REPORT,
            aggregate_id=report.id,
            workflow_id=workflow.id,
            payload={
                "report_id": report.id,
                "inspection_id": inspection.id,
                "assessed_total": total,
                "currency": report.currency,
                "line_item_count": len(data.line_items),
            },
        )
        self._notifications.enqueue_many(
            template=Template.PARTIES_DAMAGE_REPORT_READY,
            recipients=[workflow.owner_email, workflow.tenant_email],
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "property_address": workflow.property_address,
                "currency": report.currency,
                "assessed_total": total,
                "line_item_count": len(data.line_items),
                "security_deposit_amount": workflow.security_deposit_amount,
            },
        )
        await self._session.flush()
        return report

    # -- damage review (T13 step 7) ----------------------------------------
    async def get_report(self, workflow: ExitWorkflow) -> DamageReport:
        stmt = (
            select(DamageReport)
            .where(DamageReport.workflow_id == workflow.id)
            .order_by(DamageReport.submitted_at.desc())
        )
        report = (await self._session.execute(stmt)).scalars().first()
        if report is None:
            raise NotFoundError("No damage report has been uploaded for this exit workflow.")
        return report

    async def tenant_review(
        self,
        report: DamageReport,
        workflow: ExitWorkflow,
        *,
        decision: TenantReviewDecision,
        note: str | None = None,
    ) -> DamageReport:
        access.ensure_is_tenant(workflow, self._ctx.require_principal())

        if workflow.status is not ExitWorkflowStatus.DAMAGE_REVIEW:
            raise ConflictError(
                f"Exit workflow is {workflow.status.value}; the damage review window is closed."
            )
        if report.status not in (DamageReportStatus.SUBMITTED, DamageReportStatus.ACKNOWLEDGED):
            raise ConflictError(f"Damage report is already {report.status.value}.")

        now = utcnow()
        report.tenant_reviewed_at = now
        report.tenant_review_note = note

        if decision is TenantReviewDecision.DISPUTE:
            if not (note or "").strip():
                raise ValidationError(
                    "A reason is required when disputing the damage report.",
                    extra={"field": "note"},
                )
            report.status = DamageReportStatus.DISPUTED
            report.dispute_reason = note
            self._events.emit(
                self._ctx,
                event_type=EventType.DAMAGE_REPORT_DISPUTED,
                aggregate_type=AggregateType.DAMAGE_REPORT,
                aggregate_id=report.id,
                workflow_id=workflow.id,
                payload={"report_id": report.id, "reason": note},
            )
            self._notifications.enqueue(
                template=Template.OWNER_DAMAGE_DISPUTED,
                recipient=workflow.owner_email,
                workflow_id=workflow.id,
                context={
                    "reference": workflow.reference,
                    "owner_name": workflow.owner_name,
                    "property_address": workflow.property_address,
                    "dispute_reason": note,
                },
            )
        else:
            report.status = DamageReportStatus.ACKNOWLEDGED

        self._audit.record(
            self._ctx,
            action=f"damage_report.tenant_{decision.value.lower()}",
            entity_type="damage_report",
            entity_id=report.id,
            workflow_id=workflow.id,
            changes={"decision": decision, "note": note},
        )
        await self._session.flush()
        return report

    async def resolve_dispute(
        self, report: DamageReport, workflow: ExitWorkflow, *, resolution_note: str
    ) -> DamageReport:
        principal = self._ctx.require_principal()
        if not principal.is_admin:
            access.ensure_is_owner(workflow, principal)
        if report.status is not DamageReportStatus.DISPUTED:
            raise ConflictError("This damage report is not under dispute.")
        if not resolution_note.strip():
            raise ValidationError(
                "A resolution note is required.", extra={"field": "resolution_note"}
            )

        report.status = DamageReportStatus.DISPUTE_RESOLVED
        report.dispute_resolved_at = utcnow()
        report.dispute_resolved_by = principal.subject_id
        report.dispute_resolution_note = resolution_note

        self._audit.record(
            self._ctx,
            action="damage_report.dispute_resolved",
            entity_type="damage_report",
            entity_id=report.id,
            workflow_id=workflow.id,
            changes={"resolution_note": resolution_note},
        )
        self._events.emit(
            self._ctx,
            event_type=EventType.DAMAGE_REPORT_DISPUTE_RESOLVED,
            aggregate_type=AggregateType.DAMAGE_REPORT,
            aggregate_id=report.id,
            workflow_id=workflow.id,
            payload={"report_id": report.id, "resolution_note": resolution_note},
        )
        await self._session.flush()
        return report

    @staticmethod
    def ensure_ready_for_settlement(report: DamageReport) -> None:
        """A disputed report blocks the deduction until it is resolved."""

        if report.status is DamageReportStatus.DISPUTED:
            raise ConflictError(
                "The tenant has disputed the damage report; resolve the dispute before "
                "settling the deposit.",
                extra={"damage_report_id": str(report.id), "dispute_reason": report.dispute_reason},
            )
        if report.status is DamageReportStatus.FINALIZED:
            raise ConflictError("The deduction for this report has already been finalised.")
