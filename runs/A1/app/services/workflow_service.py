"""Exit workflow lifecycle: initiation (T13 steps 1-6) and closure (step 10).

Inspection (O15), settlement (O16) and NOC issuance live in their own services, but every
state change in the module funnels through :meth:`WorkflowService.transition`, which is the
single place a workflow's state is allowed to move.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.domain.states import (
    CANCELLABLE_STATES,
    ExitWorkflowState,
    assert_can_transition,
    is_terminal,
)
from app.errors import (
    ConflictError,
    NotFoundError,
    ValidationError,
    WorkflowAlreadyActive,
)
from app.models.audit import AuditAction
from app.models.base import utcnow
from app.models.catalog import Contract, ContractStatus
from app.models.workflow import (
    ActorType,
    ExitDocument,
    ExitDocumentKind,
    ExitReasonCode,
    ExitWorkflow,
    ExitWorkflowTransition,
)
from app.ports.events import EventType
from app.ports.notifications import Channel, Notification, NotificationTemplate
from app.security import PrincipalRole
from app.services.base import ServiceBase, today_in_market
from app.services.references import next_workflow_reference

S = ExitWorkflowState


@dataclass(frozen=True, slots=True)
class DocumentUpload:
    kind: ExitDocumentKind
    file_name: str
    content_type: str
    byte_size: int
    storage_key: str
    checksum_sha256: str | None = None


class WorkflowService(ServiceBase):
    # --- the single state-change chokepoint -------------------------------------------

    def transition(
        self,
        workflow: ExitWorkflow,
        target: ExitWorkflowState,
        *,
        note: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ExitWorkflowTransition:
        """Validate and apply a state change, recording it in the append-only history.

        Raises:
            InvalidStateTransition: if ``target`` is not reachable from the current state.
        """
        current = workflow.state
        assert_can_transition(current, target)

        record = ExitWorkflowTransition(
            workflow_id=workflow.id,
            from_state=current,
            to_state=target,
            actor_type=self.ctx.actor_type,
            actor_id=self.ctx.actor_id,
            note=note,
            context=context or {},
            occurred_at=utcnow(),
        )
        self.session.add(record)
        workflow.apply_state(target)
        workflow.transitions.append(record)
        return record

    def emit(
        self, workflow: ExitWorkflow, event_type: EventType, payload: dict[str, Any] | None = None
    ) -> None:
        self.events.record(
            event_type,
            aggregate_type="ExitWorkflow",
            aggregate_id=workflow.id,
            partition_key=str(workflow.property_id),
            payload={
                "workflow_id": str(workflow.id),
                "reference": workflow.reference,
                "state": workflow.state.value,
                "property_id": str(workflow.property_id),
                "tenant_id": str(workflow.tenant_id),
                "owner_id": str(workflow.owner_id),
                "contract_id": str(workflow.contract_id),
                **(payload or {}),
            },
        )

    # --- T13 steps 1-3, 5: initiation -------------------------------------------------

    async def initiate(
        self,
        *,
        contract_id: uuid.UUID,
        move_out_date: date,
        reason_code: ExitReasonCode,
        reason_text: str | None = None,
    ) -> ExitWorkflow:
        """Open an exit workflow against a contract (T13 steps 1-3, with ID generation at 5)."""
        principal = self.ctx.require_role(PrincipalRole.TENANT, PrincipalRole.ADMIN)

        contract = await self.session.get(Contract, contract_id)
        if contract is None:
            raise NotFoundError("contract not found", details={"contract_id": str(contract_id)})
        if not principal.is_admin and contract.tenant_id != principal.id:
            from app.errors import AuthorizationError

            raise AuthorizationError("caller is not the tenant on this contract")
        if contract.status is not ContractStatus.ACTIVE:
            raise ConflictError(
                "exit can only be initiated against an active contract",
                details={"contract_status": contract.status.value},
            )

        self._validate_move_out_date(move_out_date)
        if reason_code is ExitReasonCode.OTHER and not (reason_text or "").strip():
            raise ValidationError(
                "reason_text is required when reason_code is OTHER",
                details={"field": "reason_text"},
            )

        # BR-1, checked up front for a clean error; the partial unique indexes below are the
        # authority under concurrency.
        await self._assert_no_active_workflow(
            contract_id=contract.id, property_id=contract.property_id, tenant_id=contract.tenant_id
        )

        required = tuple(self.settings.required_document_kinds)
        initial_state = S.DOCUMENTS_PENDING if required else S.DRAFT

        workflow = ExitWorkflow(
            reference=await next_workflow_reference(self.session),
            contract_id=contract.id,
            property_id=contract.property_id,
            tenant_id=contract.tenant_id,
            owner_id=contract.owner_id,
            state=initial_state,
            is_active=True,
            move_out_date=move_out_date,
            reason_code=reason_code,
            reason_text=(reason_text or None),
            deposit_snapshot_fils=contract.security_deposit_fils,
            initiated_by_type=self.ctx.actor_type,
            # Initialising the collections marks them loaded, so reading them back in the
            # same transaction does not trigger a lazy load outside the async greenlet.
            documents=[],
            transitions=[],
            assignments=[],
            settlement=None,
            noc=None,
        )
        self.session.add(workflow)

        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise WorkflowAlreadyActive(
                "an exit workflow is already in progress for this contract or property",
                details={"contract_id": str(contract_id)},
            ) from exc

        creation = ExitWorkflowTransition(
            workflow_id=workflow.id,
            from_state=S.DRAFT,
            to_state=initial_state,
            actor_type=self.ctx.actor_type,
            actor_id=self.ctx.actor_id,
            note="exit workflow initiated",
            context={"required_documents": list(required)},
            occurred_at=utcnow(),
        )
        self.session.add(creation)
        workflow.transitions.append(creation)

        self.audit.record(
            AuditAction.WORKFLOW_INITIATED,
            entity_type="ExitWorkflow",
            entity_id=workflow.id,
            workflow_id=workflow.id,
            payload={
                "reference": workflow.reference,
                "move_out_date": move_out_date.isoformat(),
                "reason_code": reason_code.value,
                "deposit_snapshot_fils": workflow.deposit_snapshot_fils,
            },
        )
        self.emit(
            workflow,
            EventType.WORKFLOW_INITIATED,
            {"move_out_date": move_out_date.isoformat(), "reason_code": reason_code.value},
        )
        return workflow

    def _validate_move_out_date(self, move_out_date: date) -> None:
        today = today_in_market(self.settings)
        earliest = today + timedelta(days=self.settings.min_notice_days)
        latest = today + timedelta(days=self.settings.max_notice_days)
        if move_out_date < earliest:
            raise ValidationError(
                "move-out date is earlier than the required notice period",
                details={
                    "move_out_date": move_out_date.isoformat(),
                    "earliest_allowed": earliest.isoformat(),
                    "min_notice_days": self.settings.min_notice_days,
                },
            )
        if move_out_date > latest:
            raise ValidationError(
                "move-out date is too far in the future",
                details={
                    "move_out_date": move_out_date.isoformat(),
                    "latest_allowed": latest.isoformat(),
                },
            )

    async def _assert_no_active_workflow(
        self, *, contract_id: uuid.UUID, property_id: uuid.UUID, tenant_id: uuid.UUID
    ) -> None:
        existing = await self.session.scalar(
            sa.select(ExitWorkflow).where(
                ExitWorkflow.is_active.is_(True),
                sa.or_(
                    ExitWorkflow.contract_id == contract_id,
                    ExitWorkflow.property_id == property_id,
                    ExitWorkflow.tenant_id == tenant_id,
                ),
            )
        )
        if existing is not None:
            raise WorkflowAlreadyActive(
                "an exit workflow is already in progress",
                details={
                    "existing_workflow_id": str(existing.id),
                    "existing_reference": existing.reference,
                    "existing_state": existing.state.value,
                },
            )

    # --- T13 step 4: documents ---------------------------------------------------------

    async def upload_document(
        self, workflow_id: uuid.UUID, upload: DocumentUpload
    ) -> ExitDocument:
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_owner=False)

        if workflow.state not in (S.DRAFT, S.DOCUMENTS_PENDING):
            raise ConflictError(
                "documents can only be attached before the request is submitted",
                details={"state": workflow.state.value},
            )
        if upload.byte_size <= 0:
            raise ValidationError("document byte_size must be positive")

        document = ExitDocument(
            workflow_id=workflow.id,
            kind=upload.kind,
            file_name=upload.file_name,
            content_type=upload.content_type,
            byte_size=upload.byte_size,
            storage_key=upload.storage_key,
            checksum_sha256=upload.checksum_sha256,
            uploaded_by_type=self.ctx.actor_type,
            uploaded_by_id=self.ctx.actor_id,
            uploaded_at=utcnow(),
        )
        self.session.add(document)
        workflow.documents.append(document)
        # Flush so the generated primary key is available to the audit row and the response.
        await self.session.flush()

        self.audit.record(
            AuditAction.DOCUMENT_UPLOADED,
            entity_type="ExitDocument",
            entity_id=document.id,
            workflow_id=workflow.id,
            payload={"kind": upload.kind.value, "file_name": upload.file_name},
        )
        return document

    def missing_required_documents(self, workflow: ExitWorkflow) -> list[str]:
        required = {kind for kind in self.settings.required_document_kinds}
        present = {doc.kind.value for doc in workflow.documents}
        return sorted(required - present)

    # --- T13 step 6: submission and owner decision -------------------------------------

    async def submit(self, workflow_id: uuid.UUID) -> ExitWorkflow:
        """Tenant submits the request; the owner is notified (T13 step 6)."""
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_owner=False)

        missing = self.missing_required_documents(workflow)
        if missing:
            raise ValidationError(
                "required documents are missing",
                details={"missing_document_kinds": missing},
            )

        self.transition(workflow, S.PENDING_OWNER_APPROVAL, note="submitted for owner approval")
        workflow.submitted_at = utcnow()

        self.audit.record(
            AuditAction.WORKFLOW_SUBMITTED,
            entity_type="ExitWorkflow",
            entity_id=workflow.id,
            workflow_id=workflow.id,
            payload={"document_count": len(workflow.documents)},
        )
        self.emit(workflow, EventType.WORKFLOW_SUBMITTED)
        self.notify(
            workflow,
            Notification(
                template=NotificationTemplate.OWNER_EXIT_REQUESTED,
                channel=Channel.EMAIL,
                recipient=workflow.owner.email,
                subject=f"Exit request {workflow.reference} for {workflow.property.reference}",
                context={
                    "reference": workflow.reference,
                    "tenant_name": workflow.tenant.full_name,
                    "property": workflow.property.full_address,
                    "move_out_date": workflow.move_out_date.isoformat(),
                    "reason": workflow.reason_code.value,
                },
            ),
        )
        return workflow

    async def owner_approve(
        self,
        workflow_id: uuid.UUID,
        *,
        agency_id: uuid.UUID | None = None,
        instructions: str | None = None,
    ) -> ExitWorkflow:
        """Owner approves the exit. If an agency is named, the inspection is requested too
        (Appendix B: approval is what triggers the agency email)."""
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_tenant=False)

        self.transition(workflow, S.OWNER_APPROVED, note="owner approved the exit request")
        workflow.owner_approved_at = utcnow()

        self.audit.record(
            AuditAction.OWNER_APPROVED,
            entity_type="ExitWorkflow",
            entity_id=workflow.id,
            workflow_id=workflow.id,
            payload={"agency_id": str(agency_id) if agency_id else None},
        )
        self.emit(workflow, EventType.WORKFLOW_APPROVED)
        self.notify(
            workflow,
            Notification(
                template=NotificationTemplate.TENANT_EXIT_APPROVED,
                channel=Channel.EMAIL,
                recipient=workflow.tenant.email,
                subject=f"Your exit request {workflow.reference} was approved",
                context={"reference": workflow.reference},
            ),
        )

        if agency_id is not None:
            # Imported here to keep the two services free of a circular dependency.
            from app.services.inspection_service import InspectionService

            inspection = InspectionService(
                self.session, self.ctx, self.settings, recorder=self.events, audit=self.audit
            )
            await inspection.request_inspection(
                workflow, agency_id=agency_id, instructions=instructions
            )
        return workflow

    async def owner_reject(self, workflow_id: uuid.UUID, *, reason: str) -> ExitWorkflow:
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_tenant=False)
        if not reason.strip():
            raise ValidationError("a rejection reason is required", details={"field": "reason"})

        self.transition(workflow, S.REJECTED, note=reason)
        workflow.closed_reason = reason

        self.audit.record(
            AuditAction.OWNER_REJECTED,
            entity_type="ExitWorkflow",
            entity_id=workflow.id,
            workflow_id=workflow.id,
            payload={"reason": reason},
        )
        self.emit(workflow, EventType.WORKFLOW_REJECTED, {"reason": reason})
        self.notify(
            workflow,
            Notification(
                template=NotificationTemplate.TENANT_EXIT_REJECTED,
                channel=Channel.EMAIL,
                recipient=workflow.tenant.email,
                subject=f"Your exit request {workflow.reference} was declined",
                context={"reference": workflow.reference, "reason": reason},
            ),
        )
        return workflow

    # --- cancellation and completion ----------------------------------------------------

    async def cancel(self, workflow_id: uuid.UUID, *, reason: str) -> ExitWorkflow:
        """Abandon an in-flight workflow. Not permitted once money has moved."""
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow, allow_agency=False)
        if not reason.strip():
            raise ValidationError("a cancellation reason is required", details={"field": "reason"})
        if workflow.state not in CANCELLABLE_STATES:
            raise ConflictError(
                "this exit workflow can no longer be cancelled",
                details={
                    "state": workflow.state.value,
                    "cancellable_states": sorted(s.value for s in CANCELLABLE_STATES),
                },
            )

        self.transition(workflow, S.CANCELLED, note=reason)
        workflow.closed_reason = reason

        self.audit.record(
            AuditAction.WORKFLOW_CANCELLED,
            entity_type="ExitWorkflow",
            entity_id=workflow.id,
            workflow_id=workflow.id,
            payload={"reason": reason},
        )
        self.emit(workflow, EventType.WORKFLOW_CANCELLED, {"reason": reason})
        return workflow

    async def complete(
        self, workflow: ExitWorkflow, *, note: str = "exit workflow completed"
    ) -> ExitWorkflow:
        """Close the workflow (T13 step 10), releasing the BR-1 contract lock.

        Called automatically on first NOC download, and available explicitly so a tenant who
        never downloads cannot leave the property blocked indefinitely.
        """
        self.transition(workflow, S.COMPLETED, note=note)
        workflow.completed_at = utcnow()

        self.audit.record(
            AuditAction.WORKFLOW_COMPLETED,
            entity_type="ExitWorkflow",
            entity_id=workflow.id,
            workflow_id=workflow.id,
            payload={"note": note},
        )
        self.emit(workflow, EventType.WORKFLOW_COMPLETED)
        for recipient in (workflow.tenant.email, workflow.owner.email):
            self.notify(
                workflow,
                Notification(
                    template=NotificationTemplate.PARTIES_WORKFLOW_COMPLETED,
                    channel=Channel.EMAIL,
                    recipient=recipient,
                    subject=f"Exit workflow {workflow.reference} is complete",
                    context={"reference": workflow.reference},
                ),
            )
        return workflow

    async def complete_by_id(self, workflow_id: uuid.UUID) -> ExitWorkflow:
        workflow = await self.load_workflow(workflow_id, for_update=True)
        self.authorize_participant(workflow)
        return await self.complete(workflow)

    # --- reads --------------------------------------------------------------------------

    async def get(self, workflow_id: uuid.UUID) -> ExitWorkflow:
        workflow = await self.load_workflow(workflow_id)
        self.authorize_participant(workflow, allow_agency=True)
        return workflow

    async def list_for_principal(
        self, *, active_only: bool = False, limit: int = 50, offset: int = 0
    ) -> list[ExitWorkflow]:
        principal = self.ctx.require_principal()
        stmt = sa.select(ExitWorkflow).order_by(ExitWorkflow.created_at.desc())

        match principal.role:
            case PrincipalRole.TENANT:
                stmt = stmt.where(ExitWorkflow.tenant_id == principal.id)
            case PrincipalRole.OWNER:
                stmt = stmt.where(ExitWorkflow.owner_id == principal.id)
            case PrincipalRole.AGENCY:
                from app.models.inspection import InspectionAssignment

                agency_id = principal.agency_id or principal.id
                stmt = stmt.where(
                    ExitWorkflow.id.in_(
                        sa.select(InspectionAssignment.workflow_id).where(
                            InspectionAssignment.agency_id == agency_id
                        )
                    )
                )
            case PrincipalRole.ADMIN:
                pass

        if active_only:
            stmt = stmt.where(ExitWorkflow.is_active.is_(True))
        stmt = stmt.limit(min(limit, 200)).offset(max(offset, 0))
        return list((await self.session.scalars(stmt)).all())

    @staticmethod
    def is_closed(workflow: ExitWorkflow) -> bool:
        return is_terminal(workflow.state)


__all__ = ["ActorType", "DocumentUpload", "WorkflowService"]
