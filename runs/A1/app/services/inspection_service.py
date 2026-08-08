"""Third-party inspection workflow (SRS O15) and damage reporting (O16).

Implements the Appendix B sequence end to end: agency notified with property details ->
agency proposes available dates -> owner or tenant selects one -> inspection occurs -> report
with photos uploaded -> deductions computed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from app.domain.states import ExitWorkflowState
from app.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from app.models.audit import AuditAction
from app.models.base import utcnow
from app.models.catalog import InspectionAgency
from app.models.inspection import (
    AssignmentStatus,
    DamageLineItem,
    DamageReport,
    DamageSeverity,
    InspectionAssignment,
    InspectionSlot,
)
from app.models.workflow import ExitWorkflow
from app.ports.events import EventType
from app.ports.notifications import Channel, Notification, NotificationTemplate
from app.security import PrincipalRole
from app.services.base import ServiceBase
from app.services.workflow_service import WorkflowService

S = ExitWorkflowState

MAX_SLOTS_PER_PROPOSAL = 10


@dataclass(frozen=True, slots=True)
class SlotProposal:
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True, slots=True)
class DamageLineInput:
    code: str
    description: str
    severity: DamageSeverity
    amount_fils: int
    location: str | None = None
    photos: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DamageReportInput:
    summary: str
    inspected_at: datetime
    line_items: list[DamageLineInput]
    inspector_name: str | None = None
    photos: list[dict[str, Any]] = field(default_factory=list)


class InspectionService(ServiceBase):
    def _workflow_service(self) -> WorkflowService:
        return WorkflowService(
            self.session, self.ctx, self.settings, recorder=self.events, audit=self.audit
        )

    # --- assignment -------------------------------------------------------------------

    async def request_inspection(
        self,
        workflow: ExitWorkflow,
        *,
        agency_id: uuid.UUID,
        instructions: str | None = None,
    ) -> InspectionAssignment:
        """Assign a registered agency and email it the property details (O15)."""
        agency = await self.session.get(InspectionAgency, agency_id)
        if agency is None:
            raise NotFoundError("inspection agency not found", details={"agency_id": str(agency_id)})
        if not agency.is_active:
            raise ConflictError(
                "inspection agency is not active", details={"agency_id": str(agency_id)}
            )

        workflows = self._workflow_service()
        workflows.transition(
            workflow,
            S.INSPECTION_SCHEDULING,
            note=f"inspection requested from {agency.name}",
            context={"agency_id": str(agency_id)},
        )

        attempt = max((a.attempt for a in workflow.assignments), default=0) + 1
        assignment = InspectionAssignment(
            workflow_id=workflow.id,
            agency_id=agency.id,
            attempt=attempt,
            status=AssignmentStatus.REQUESTED,
            requested_at=utcnow(),
            notified_at=utcnow(),
            instructions=instructions,
            slots=[],
            report=None,
        )
        self.session.add(assignment)
        workflow.assignments.append(assignment)
        await self.session.flush()

        self.audit.record(
            AuditAction.INSPECTION_REQUESTED,
            entity_type="InspectionAssignment",
            entity_id=assignment.id,
            workflow_id=workflow.id,
            payload={"agency_id": str(agency.id), "attempt": attempt},
        )
        workflows.emit(
            workflow,
            EventType.INSPECTION_REQUESTED,
            {"agency_id": str(agency.id), "assignment_id": str(assignment.id), "attempt": attempt},
        )
        self.notify(
            workflow,
            Notification(
                template=NotificationTemplate.AGENCY_INSPECTION_REQUESTED,
                channel=Channel.EMAIL,
                recipient=agency.email,
                subject=f"Inspection request {workflow.reference}",
                context={
                    "reference": workflow.reference,
                    "assignment_id": str(assignment.id),
                    "property_reference": workflow.property.reference,
                    "property_address": workflow.property.full_address,
                    "move_out_date": workflow.move_out_date.isoformat(),
                    "tenant_name": workflow.tenant.full_name,
                    "owner_name": workflow.owner.full_name,
                    "instructions": instructions,
                },
            ),
        )
        return assignment

    async def request_inspection_by_id(
        self,
        workflow_id: uuid.UUID,
        *,
        agency_id: uuid.UUID,
        instructions: str | None = None,
    ) -> InspectionAssignment:
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_tenant=False)
        return await self.request_inspection(
            workflow, agency_id=agency_id, instructions=instructions
        )

    # --- agency-facing ------------------------------------------------------------------

    async def _load_assignment_for_agency(self, assignment_id: uuid.UUID) -> InspectionAssignment:
        assignment = await self.session.get(InspectionAssignment, assignment_id)
        if assignment is None:
            raise NotFoundError(
                "inspection assignment not found", details={"assignment_id": str(assignment_id)}
            )
        principal = self.ctx.require_principal()
        if principal.is_admin:
            return assignment
        if principal.role is not PrincipalRole.AGENCY:
            raise AuthorizationError("this endpoint is for inspection agencies")
        agency_id = principal.agency_id or principal.id
        if assignment.agency_id != agency_id:
            raise AuthorizationError("assignment belongs to a different agency")
        return assignment

    async def propose_slots(
        self, assignment_id: uuid.UUID, proposals: list[SlotProposal]
    ) -> InspectionAssignment:
        """Agency responds with available dates (Appendix B)."""
        assignment = await self._load_assignment_for_agency(assignment_id)
        workflow = await self.load_workflow(assignment.workflow_id, for_update=True)

        if workflow.state is not S.INSPECTION_SCHEDULING:
            raise ConflictError(
                "slots can only be proposed while the inspection is being scheduled",
                details={"state": workflow.state.value},
            )
        if assignment.status not in (AssignmentStatus.REQUESTED, AssignmentStatus.SLOTS_PROPOSED):
            raise ConflictError(
                "this assignment is no longer accepting slot proposals",
                details={"assignment_status": assignment.status.value},
            )
        if not proposals:
            raise ValidationError("at least one slot must be proposed")
        if len(proposals) > MAX_SLOTS_PER_PROPOSAL:
            raise ValidationError(
                f"at most {MAX_SLOTS_PER_PROPOSAL} slots may be proposed at once",
                details={"count": len(proposals)},
            )

        now = utcnow()
        existing = {(s.starts_at, s.ends_at) for s in assignment.slots}
        added = 0
        for proposal in proposals:
            if proposal.ends_at <= proposal.starts_at:
                raise ValidationError(
                    "slot end must be after its start",
                    details={"starts_at": proposal.starts_at.isoformat()},
                )
            if proposal.starts_at <= now:
                raise ValidationError(
                    "proposed slots must be in the future",
                    details={"starts_at": proposal.starts_at.isoformat()},
                )
            if (proposal.starts_at, proposal.ends_at) in existing:
                continue
            slot = InspectionSlot(
                assignment_id=assignment.id,
                starts_at=proposal.starts_at,
                ends_at=proposal.ends_at,
                proposed_at=now,
            )
            self.session.add(slot)
            assignment.slots.append(slot)
            added += 1

        assignment.status = AssignmentStatus.SLOTS_PROPOSED
        await self.session.flush()

        self.audit.record(
            AuditAction.INSPECTION_SLOTS_PROPOSED,
            entity_type="InspectionAssignment",
            entity_id=assignment.id,
            workflow_id=workflow.id,
            payload={"slots_added": added, "slots_total": len(assignment.slots)},
        )
        self._workflow_service().emit(
            workflow,
            EventType.INSPECTION_SLOTS_PROPOSED,
            {"assignment_id": str(assignment.id), "slot_count": len(assignment.slots)},
        )
        for recipient in (workflow.tenant.email, workflow.owner.email):
            self.notify(
                workflow,
                Notification(
                    template=NotificationTemplate.PARTIES_SLOTS_PROPOSED,
                    channel=Channel.EMAIL,
                    recipient=recipient,
                    subject=f"Inspection dates available for {workflow.reference}",
                    context={
                        "reference": workflow.reference,
                        "slots": [
                            {"starts_at": s.starts_at.isoformat(), "ends_at": s.ends_at.isoformat()}
                            for s in assignment.slots
                        ],
                    },
                ),
            )
        return assignment

    async def submit_damage_report(
        self, assignment_id: uuid.UUID, payload: DamageReportInput
    ) -> DamageReport:
        """Agency uploads the damage report with photos; deductions are computed (O16)."""
        assignment = await self._load_assignment_for_agency(assignment_id)
        workflow = await self.load_workflow(assignment.workflow_id, for_update=True)
        workflows = self._workflow_service()

        if assignment.report is not None:
            raise ConflictError(
                "a damage report has already been submitted for this assignment",
                details={"report_id": str(assignment.report.id)},
            )
        if not payload.summary.strip():
            raise ValidationError("report summary is required")
        if payload.inspected_at > utcnow():
            raise ValidationError("inspected_at cannot be in the future")

        # The report is itself evidence the inspection happened, so accept it directly from
        # SCHEDULED and record the intermediate transition rather than forcing an extra call.
        if workflow.state is S.INSPECTION_SCHEDULED:
            await self.mark_inspection_completed(workflow, assignment, note="report submitted")
        if workflow.state is not S.INSPECTION_COMPLETED:
            raise ConflictError(
                "a damage report can only be submitted after the inspection is completed",
                details={"state": workflow.state.value},
            )

        for item in payload.line_items:
            if item.amount_fils < 0:
                raise ValidationError(
                    "deduction amounts cannot be negative", details={"code": item.code}
                )

        total = sum(item.amount_fils for item in payload.line_items)
        report = DamageReport(
            assignment_id=assignment.id,
            workflow_id=workflow.id,
            agency_id=assignment.agency_id,
            summary=payload.summary.strip(),
            inspector_name=payload.inspector_name,
            inspected_at=payload.inspected_at,
            submitted_at=utcnow(),
            total_deductions_fils=total,
            photos=payload.photos,
            line_items=[],
        )
        self.session.add(report)
        await self.session.flush()

        for item in payload.line_items:
            line = DamageLineItem(
                report_id=report.id,
                code=item.code,
                description=item.description,
                location=item.location,
                severity=item.severity,
                amount_fils=item.amount_fils,
                photos=item.photos,
            )
            self.session.add(line)
            report.line_items.append(line)
        assignment.report = report
        assignment.status = AssignmentStatus.COMPLETED
        await self.session.flush()

        workflows.transition(
            workflow,
            S.DAMAGE_REVIEW,
            note="damage report submitted",
            context={"report_id": str(report.id), "total_deductions_fils": total},
        )

        self.audit.record(
            AuditAction.DAMAGE_REPORT_SUBMITTED,
            entity_type="DamageReport",
            entity_id=report.id,
            workflow_id=workflow.id,
            payload={
                "total_deductions_fils": total,
                "line_item_count": len(payload.line_items),
                "agency_id": str(assignment.agency_id),
            },
        )
        workflows.emit(
            workflow,
            EventType.DAMAGE_REPORT_SUBMITTED,
            {"report_id": str(report.id), "total_deductions_fils": total},
        )

        # Compute the settlement immediately so both parties see the figures during review.
        from app.services.settlement_service import SettlementService

        settlements = SettlementService(
            self.session, self.ctx, self.settings, recorder=self.events, audit=self.audit
        )
        await settlements.compute_for_report(workflow, report)

        for recipient in (workflow.tenant.email, workflow.owner.email):
            self.notify(
                workflow,
                Notification(
                    template=NotificationTemplate.PARTIES_DAMAGE_REPORT_READY,
                    channel=Channel.EMAIL,
                    recipient=recipient,
                    subject=f"Damage report ready for {workflow.reference}",
                    context={
                        "reference": workflow.reference,
                        "total_deductions_fils": total,
                        "line_item_count": len(payload.line_items),
                    },
                ),
            )
        return report

    # --- party-facing --------------------------------------------------------------------

    async def select_slot(self, workflow_id: uuid.UUID, slot_id: uuid.UUID) -> InspectionAssignment:
        """Owner or tenant confirms one of the proposed appointment windows."""
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow)

        assignment = workflow.current_assignment
        if assignment is None:
            raise ConflictError("no inspection has been requested for this workflow")
        if workflow.state is not S.INSPECTION_SCHEDULING:
            raise ConflictError(
                "a slot can only be selected while the inspection is being scheduled",
                details={"state": workflow.state.value},
            )

        slot = next((s for s in assignment.slots if s.id == slot_id), None)
        if slot is None:
            raise NotFoundError(
                "slot not found on the current assignment", details={"slot_id": str(slot_id)}
            )
        if slot.starts_at <= utcnow():
            raise ValidationError("cannot select a slot that has already started")

        for other in assignment.slots:
            other.is_selected = other.id == slot.id
        slot.selected_at = utcnow()
        slot.selected_by_type = self.ctx.actor_type
        slot.selected_by_id = self.ctx.actor_id

        assignment.status = AssignmentStatus.SCHEDULED
        assignment.scheduled_start = slot.starts_at
        assignment.scheduled_end = slot.ends_at

        workflows = self._workflow_service()
        workflows.transition(
            workflow,
            S.INSPECTION_SCHEDULED,
            note="inspection appointment confirmed",
            context={"slot_id": str(slot.id), "starts_at": slot.starts_at.isoformat()},
        )

        self.audit.record(
            AuditAction.INSPECTION_SCHEDULED,
            entity_type="InspectionAssignment",
            entity_id=assignment.id,
            workflow_id=workflow.id,
            payload={"slot_id": str(slot.id), "starts_at": slot.starts_at.isoformat()},
        )
        workflows.emit(
            workflow,
            EventType.INSPECTION_SCHEDULED,
            {"assignment_id": str(assignment.id), "starts_at": slot.starts_at.isoformat()},
        )
        agency_email = assignment.agency.email
        for recipient in (workflow.tenant.email, workflow.owner.email, agency_email):
            self.notify(
                workflow,
                Notification(
                    template=NotificationTemplate.PARTIES_INSPECTION_SCHEDULED,
                    channel=Channel.EMAIL,
                    recipient=recipient,
                    subject=f"Inspection scheduled for {workflow.reference}",
                    context={
                        "reference": workflow.reference,
                        "starts_at": slot.starts_at.isoformat(),
                        "ends_at": slot.ends_at.isoformat(),
                        "property_address": workflow.property.full_address,
                    },
                ),
            )
        return assignment

    async def mark_inspection_completed(
        self,
        workflow: ExitWorkflow,
        assignment: InspectionAssignment,
        *,
        note: str = "inspection completed",
    ) -> None:
        workflows = self._workflow_service()
        workflows.transition(workflow, S.INSPECTION_COMPLETED, note=note)
        workflow.inspection_completed_at = utcnow()
        assignment.completed_at = utcnow()

        self.audit.record(
            AuditAction.INSPECTION_COMPLETED,
            entity_type="InspectionAssignment",
            entity_id=assignment.id,
            workflow_id=workflow.id,
            payload={"note": note},
        )
        workflows.emit(
            workflow, EventType.INSPECTION_COMPLETED, {"assignment_id": str(assignment.id)}
        )

    async def complete_inspection(self, assignment_id: uuid.UUID) -> InspectionAssignment:
        """Agency marks the visit as having taken place, ahead of uploading the report."""
        assignment = await self._load_assignment_for_agency(assignment_id)
        workflow = await self.load_workflow(assignment.workflow_id, for_update=True)
        await self.mark_inspection_completed(workflow, assignment)
        return assignment

    async def order_reinspection(
        self, workflow_id: uuid.UUID, *, agency_id: uuid.UUID, reason: str
    ) -> InspectionAssignment:
        """Send the workflow back for another inspection after a disputed report."""
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_tenant=False)
        if not reason.strip():
            raise ValidationError("a reason is required to order a re-inspection")
        if workflow.state is not S.DAMAGE_REVIEW:
            raise ConflictError(
                "a re-inspection can only be ordered while the damage report is under review",
                details={"state": workflow.state.value},
            )

        # Void the settlement drafted from the superseded report before re-inspecting.
        from app.services.settlement_service import SettlementService

        settlements = SettlementService(
            self.session, self.ctx, self.settings, recorder=self.events, audit=self.audit
        )
        await settlements.void_for_reinspection(workflow, reason=reason)

        return await self.request_inspection(workflow, agency_id=agency_id, instructions=reason)

    # --- reads -----------------------------------------------------------------------------

    async def list_agency_assignments(
        self, *, status: AssignmentStatus | None = None, limit: int = 50
    ) -> list[InspectionAssignment]:
        principal = self.ctx.require_principal()
        if principal.role is not PrincipalRole.AGENCY and not principal.is_admin:
            raise AuthorizationError("this endpoint is for inspection agencies")

        stmt = sa.select(InspectionAssignment).order_by(InspectionAssignment.requested_at.desc())
        if principal.role is PrincipalRole.AGENCY:
            stmt = stmt.where(
                InspectionAssignment.agency_id == (principal.agency_id or principal.id)
            )
        if status is not None:
            stmt = stmt.where(InspectionAssignment.status == status)
        return list((await self.session.scalars(stmt.limit(min(limit, 200)))).all())
