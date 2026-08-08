"""Exit workflow service — T13 steps 1-5, plus approval and cancellation."""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.core.clock import today, utcnow
from exit_workflow.core.config import Settings
from exit_workflow.core.errors import (
    BusinessRuleViolation,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from exit_workflow.core.ids import workflow_reference
from exit_workflow.core.security import Role
from exit_workflow.domain import policy
from exit_workflow.domain.enums import (
    ACTIVE_STATUSES,
    DocumentType,
    ExitReason,
    ExitWorkflowStatus,
)
from exit_workflow.integrations.contracts import ContractDirectory, ContractSnapshot
from exit_workflow.models.audit import WorkflowTransition
from exit_workflow.models.document import Document
from exit_workflow.models.workflow import ExitWorkflow
from exit_workflow.services import access
from exit_workflow.services.audit import AuditRecorder
from exit_workflow.services.context import ServiceContext
from exit_workflow.services.eligibility import EligibilityService, RULE_ID
from exit_workflow.services.events import AggregateType, EventRecorder, EventType
from exit_workflow.services.notifications import NotificationService, Template
from exit_workflow.services.transitions import apply_transition, workflow_event_payload

_REFERENCE_RE = re.compile(r"^[A-Z]{2,5}-\d{4}-[0-9A-Z]{4,10}$")
_MAX_REFERENCE_ATTEMPTS = 5


def _constraint_name(exc: IntegrityError) -> str | None:
    """Extract the violated constraint from an asyncpg/psycopg error."""

    orig = getattr(exc, "orig", None)
    cause = getattr(orig, "__cause__", None) or orig
    name = getattr(cause, "constraint_name", None)
    if name:
        return str(name)
    match = re.search(r'"([^"]+)"', str(exc))
    return match.group(1) if match else None


class ExitWorkflowService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        ctx: ServiceContext,
        *,
        audit: AuditRecorder,
        events: EventRecorder,
        notifications: NotificationService,
        contracts: ContractDirectory,
        eligibility: EligibilityService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._ctx = ctx
        self._audit = audit
        self._events = events
        self._notifications = notifications
        self._contracts = contracts
        self._eligibility = eligibility

    # -- lookups ---------------------------------------------------------
    async def get(self, identifier: uuid.UUID | str, *, for_update: bool = False) -> ExitWorkflow:
        """Fetch by id or by human reference, then authorise."""

        stmt = select(ExitWorkflow)
        if isinstance(identifier, uuid.UUID):
            stmt = stmt.where(ExitWorkflow.id == identifier)
        else:
            candidate = str(identifier).strip().upper()
            if not _REFERENCE_RE.match(candidate):
                raise NotFoundError("Exit workflow not found.")
            stmt = stmt.where(ExitWorkflow.reference == candidate)
        if for_update:
            # Serialises concurrent actions on one workflow; every mutating
            # path takes this lock before evaluating the state machine.
            stmt = stmt.with_for_update(of=ExitWorkflow)

        workflow = (await self._session.execute(stmt)).scalars().first()
        if workflow is None:
            raise NotFoundError("Exit workflow not found.")
        access.ensure_can_view(workflow, self._ctx.require_principal())
        return workflow

    async def list(
        self,
        *,
        statuses: list[ExitWorkflowStatus] | None = None,
        property_id: uuid.UUID | None = None,
        contract_id: uuid.UUID | None = None,
        tenant_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        active_only: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[ExitWorkflow], int]:
        principal = self._ctx.require_principal()
        stmt = select(ExitWorkflow)

        # Scope narrowing is applied server-side; a client cannot widen it.
        match principal.role:
            case Role.TENANT:
                stmt = stmt.where(ExitWorkflow.tenant_id == principal.subject_id)
            case Role.OWNER:
                stmt = stmt.where(ExitWorkflow.owner_id == principal.subject_id)
            case Role.INSPECTION_AGENCY:
                from exit_workflow.models.inspection import Inspection

                stmt = stmt.where(
                    ExitWorkflow.id.in_(
                        select(Inspection.workflow_id).where(
                            Inspection.agency_id == principal.agency_scope()
                        )
                    )
                )
            case _:
                pass

        if statuses:
            stmt = stmt.where(ExitWorkflow.status.in_(statuses))
        if active_only:
            stmt = stmt.where(ExitWorkflow.status.in_(ACTIVE_STATUSES))
        if property_id:
            stmt = stmt.where(ExitWorkflow.property_id == property_id)
        if contract_id:
            stmt = stmt.where(ExitWorkflow.contract_id == contract_id)
        if tenant_id:
            stmt = stmt.where(ExitWorkflow.tenant_id == tenant_id)
        if owner_id:
            stmt = stmt.where(ExitWorkflow.owner_id == owner_id)

        total = (
            await self._session.execute(
                select(func.count()).select_from(stmt.order_by(None).subquery())
            )
        ).scalar_one()
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(ExitWorkflow.created_at.desc()).limit(limit).offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def timeline(self, workflow: ExitWorkflow) -> list[WorkflowTransition]:
        stmt = (
            select(WorkflowTransition)
            .where(WorkflowTransition.workflow_id == workflow.id)
            .order_by(WorkflowTransition.occurred_at, WorkflowTransition.id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # -- T13 steps 1-4: initiation ----------------------------------------
    async def initiate(
        self,
        *,
        contract_id: uuid.UUID,
        move_out_date: date,
        reason: ExitReason,
        reason_details: str | None = None,
    ) -> ExitWorkflow:
        principal = self._ctx.require_principal()
        principal.require_role(Role.TENANT)

        if reason is ExitReason.OTHER and not (reason_details or "").strip():
            raise ValidationError(
                "reason_details is required when reason is OTHER.",
                extra={"field": "reason_details"},
            )

        contract = await self._contracts.get_contract(contract_id)
        contract.ensure_exitable()

        if principal.role is Role.TENANT and contract.tenant_id != principal.subject_id:
            # Do not disclose that someone else's contract exists.
            raise NotFoundError(f"Contract {contract_id} was not found.")

        policy.validate_move_out_date(move_out_date, today(), self._settings)
        await self._assert_br1_clear(contract)

        workflow = await self._insert_workflow(contract, move_out_date, reason, reason_details)

        self._audit.record(
            self._ctx,
            action="exit_workflow.initiated",
            entity_type="exit_workflow",
            entity_id=workflow.id,
            workflow_id=workflow.id,
            changes={
                "reference": workflow.reference,
                "contract_id": contract_id,
                "move_out_date": move_out_date,
                "reason": reason,
                "security_deposit_amount": workflow.security_deposit_amount,
            },
        )
        payload = workflow_event_payload(workflow, move_out_date=move_out_date, reason=reason.value)
        self._events.emit(
            self._ctx,
            event_type=EventType.WORKFLOW_INITIATED,
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id=workflow.id,
            workflow_id=workflow.id,
            payload=payload,
        )
        # BR-1: from this moment the property and the tenant are locked.
        self._events.emit(
            self._ctx,
            event_type=EventType.LOCK_ACQUIRED,
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id=workflow.id,
            workflow_id=workflow.id,
            payload=payload,
        )
        return workflow

    async def _assert_br1_clear(self, contract: ContractSnapshot) -> None:
        result = await self._eligibility.check_contract_creation(
            property_id=contract.property_id, tenant_id=contract.tenant_id
        )
        if not result.allowed:
            raise BusinessRuleViolation(
                RULE_ID,
                " ".join(result.warning_messages),
                extra={
                    "blocks": [
                        {
                            "subject": b.subject,
                            "workflow_id": str(b.workflow_id),
                            "workflow_reference": b.workflow_reference,
                            "workflow_status": b.workflow_status,
                            "message": b.message,
                        }
                        for b in result.blocks
                    ]
                },
            )

    async def _insert_workflow(
        self,
        contract: ContractSnapshot,
        move_out_date: date,
        reason: ExitReason,
        reason_details: str | None,
    ) -> ExitWorkflow:
        now = utcnow()
        for attempt in range(_MAX_REFERENCE_ATTEMPTS):
            workflow = ExitWorkflow(
                reference=workflow_reference(self._settings.workflow_reference_prefix, now.year),
                contract_id=contract.contract_id,
                property_id=contract.property_id,
                tenant_id=contract.tenant_id,
                owner_id=contract.owner_id,
                property_reference=contract.property_reference,
                property_address=contract.property_address,
                tenant_name=contract.tenant_name,
                tenant_email=contract.tenant_email,
                owner_name=contract.owner_name,
                owner_email=contract.owner_email,
                status=ExitWorkflowStatus.INITIATED,
                move_out_date=move_out_date,
                reason=reason,
                reason_details=(reason_details or None),
                security_deposit_amount=contract.security_deposit_amount,
                currency=(contract.currency or self._settings.currency).upper(),
                initiated_at=now,
                initiated_by=self._ctx.require_principal().subject_id,
            )
            self._session.add(workflow)
            try:
                async with self._session.begin_nested():
                    await self._session.flush()
            except IntegrityError as exc:
                constraint = _constraint_name(exc) or ""
                if "reference" in constraint and attempt < _MAX_REFERENCE_ATTEMPTS - 1:
                    self._session.expunge(workflow)
                    continue
                raise self._translate_initiation_conflict(exc, constraint, contract) from exc
            return workflow
        raise ConflictError(  # pragma: no cover - astronomically unlikely
            "Could not allocate a unique workflow reference; please retry."
        )

    def _translate_initiation_conflict(
        self, exc: IntegrityError, constraint: str, contract: ContractSnapshot
    ) -> Exception:
        """Turn a partial-unique-index race into the BR-1 message."""

        if "active_property" in constraint:
            return BusinessRuleViolation(
                RULE_ID,
                "An exit workflow is already in progress for this property and must be "
                "COMPLETE before another can start.",
                extra={"property_id": str(contract.property_id)},
            )
        if "active_tenant" in constraint:
            return BusinessRuleViolation(
                RULE_ID,
                "This tenant already has an exit workflow in progress; it must be fully "
                "completed first.",
                extra={"tenant_id": str(contract.tenant_id)},
            )
        if "active_contract" in constraint:
            return ConflictError(
                "An exit workflow already exists for this contract.",
                extra={"contract_id": str(contract.contract_id)},
            )
        return ConflictError("Exit workflow could not be created due to a conflicting record.")

    # -- T13 step 5: submission and owner notification ---------------------
    async def submit(self, workflow: ExitWorkflow) -> ExitWorkflow:
        principal = self._ctx.require_principal()
        access.ensure_is_tenant(workflow, principal)

        await self._assert_required_documents(workflow)

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.PENDING_OWNER_APPROVAL,
            reason="Tenant submitted the exit request",
        )
        workflow.submitted_at = utcnow()

        self._notifications.enqueue(
            template=Template.OWNER_EXIT_REQUESTED,
            recipient=workflow.owner_email,
            workflow_id=workflow.id,
            context={
                "reference": workflow.reference,
                "owner_name": workflow.owner_name,
                "tenant_name": workflow.tenant_name,
                "property_address": workflow.property_address,
                "property_reference": workflow.property_reference,
                "move_out_date": workflow.move_out_date,
                "reason": workflow.reason.value,
                "document_count": workflow.documents_uploaded_count,
            },
        )
        await self._session.flush()
        return workflow

    async def _assert_required_documents(self, workflow: ExitWorkflow) -> None:
        required = self._settings.required_document_types
        if not required:
            return
        rows = (
            await self._session.execute(
                select(Document.document_type).where(Document.workflow_id == workflow.id).distinct()
            )
        ).scalars().all()
        missing = policy.missing_required_documents(set(rows), self._settings)
        if missing:
            raise ValidationError(
                "Required documents are missing: "
                + ", ".join(DocumentType(m).value for m in missing),
                extra={"missing_document_types": [DocumentType(m).value for m in missing]},
            )

    # -- owner decision -----------------------------------------------------
    async def approve(self, workflow: ExitWorkflow, *, note: str | None = None) -> ExitWorkflow:
        access.ensure_is_owner(workflow, self._ctx.require_principal())
        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.OWNER_APPROVED,
            reason=note or "Owner approved the exit",
        )
        workflow.owner_decision_at = utcnow()
        workflow.owner_decision_by = self._ctx.actor_id

        self._notifications.enqueue(
            template=Template.TENANT_EXIT_APPROVED,
            recipient=workflow.tenant_email,
            workflow_id=workflow.id,
            context=self._party_context(workflow),
        )
        await self._session.flush()
        return workflow

    async def reject(self, workflow: ExitWorkflow, *, reason: str) -> ExitWorkflow:
        access.ensure_is_owner(workflow, self._ctx.require_principal())
        if not reason.strip():
            raise ValidationError("A rejection reason is required.", extra={"field": "reason"})

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.REJECTED,
            reason=reason,
        )
        workflow.owner_decision_at = utcnow()
        workflow.owner_decision_by = self._ctx.actor_id
        workflow.owner_rejection_reason = reason

        self._notifications.enqueue(
            template=Template.TENANT_EXIT_REJECTED,
            recipient=workflow.tenant_email,
            workflow_id=workflow.id,
            context=self._party_context(workflow) | {"rejection_reason": reason},
        )
        await self._session.flush()
        return workflow

    async def cancel(self, workflow: ExitWorkflow, *, reason: str) -> ExitWorkflow:
        principal = self._ctx.require_principal()
        if not reason.strip():
            raise ValidationError("A cancellation reason is required.", extra={"field": "reason"})
        if principal.role is Role.INSPECTION_AGENCY:
            raise ForbiddenError("An inspection agency cannot cancel an exit workflow.")
        access.ensure_is_party(workflow, principal)

        apply_transition(
            self._session,
            self._ctx,
            self._audit,
            self._events,
            workflow,
            ExitWorkflowStatus.CANCELLED,
            reason=reason,
        )
        await self._session.flush()
        return workflow

    def _party_context(self, workflow: ExitWorkflow) -> dict[str, Any]:
        return {
            "reference": workflow.reference,
            "tenant_name": workflow.tenant_name,
            "owner_name": workflow.owner_name,
            "property_address": workflow.property_address,
            "property_reference": workflow.property_reference,
            "move_out_date": workflow.move_out_date,
        }
