"""Initiation — algorithm.md steps 1 to 5.

    1. Tenant opens exit. Load contract. Assert status == ACTIVE, else 422. (EXIT-01)
    2. Assert no existing workflow for contract, else 409 with existing ID. (EXIT-01, X-001)
    3. Validate move_out_date >= today in Asia/Dubai, reason in reference list,
       documents >= 1. (EXIT-02, X-007)
    4. IN ONE TRANSACTION: insert workflow (INITIATED->DOCS_SUBMITTED), set
       property.exitLock = true, write audit row. (EXIT-03)
    5. AFTER COMMIT: emit owner notification event. (EXIT-04, X-002)

Step 5 is not this service's to perform: it must happen after the caller's
transaction commits. :meth:`ExitInitiationService.initiate` queues the event in
the same transaction and returns its ID; the route dispatches it once the
session has committed.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.config import Settings, get_settings
from exit_workflow.db import errors as db_errors
from exit_workflow.db.models import ExitWorkflow
from exit_workflow.domain import clock as clock_module
from exit_workflow.domain.clock import Clock, DEFAULT_CLOCK
from exit_workflow.domain.enums import ActorRole, ContractStatus, EventType
from exit_workflow.domain.errors import (
    AuthorizationError,
    DocumentsRequired,
    ExitAlreadyInProgress,
    MoveOutDateInPast,
    UndefinedErrorCode,
    WorkflowNotFound,
)
from exit_workflow.domain.principal import Principal
from exit_workflow.domain.reasons import ExitReasonReference, validate_reason
from exit_workflow.domain.states import State
from exit_workflow.repositories.audit import AuditRepository
from exit_workflow.repositories.contracts import ContractRepository
from exit_workflow.repositories.outbox import OutboxRepository
from exit_workflow.repositories.properties import PropertyRepository
from exit_workflow.repositories.workflows import WorkflowRepository
from exit_workflow.services.transitions import apply_transition

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InitiateExitCommand:
    """api.yaml POST /exit-workflows request body."""

    contract_id: uuid.UUID
    move_out_date: date
    reason: str
    documents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InitiationResult:
    """What initiation produced, detached from the session."""

    workflow_id: str
    #: The state persisted by the initiation transaction: DOCS_SUBMITTED
    #: (algorithm.md step 4). api.yaml declares the 201 body as
    #: ``status: INITIATED``; see blockers.md#B-8 for the conflict.
    persisted_status: State
    #: Owner notification queued in the same transaction, to be dispatched after
    #: commit (rules.yaml#EXIT-04).
    notification_event_id: uuid.UUID


class ExitInitiationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        reasons: ExitReasonReference,
        settings: Settings | None = None,
        clock: Clock = DEFAULT_CLOCK,
    ) -> None:
        self._session = session
        self._reasons = reasons
        self._settings = settings or get_settings()
        self._clock = clock

    async def initiate(self, command: InitiateExitCommand, actor: Principal) -> InitiationResult:
        """Run steps 1 to 4 in the caller's transaction."""
        contract = await ContractRepository(self._session).get(
            command.contract_id, for_update=True
        )

        # api.yaml authz: "tenant, own active contract only". A contract that
        # does not exist and one that belongs to somebody else are reported
        # identically, so this endpoint cannot be used to enumerate contracts.
        if contract is None or (actor.role is ActorRole.TENANT and contract.tenant_id != actor.uuid):
            raise WorkflowNotFound("Contract not found.")
        if actor.role is not ActorRole.TENANT:
            raise AuthorizationError("Only the tenant may initiate an exit workflow.")

        # algorithm.md step 1 / rules.yaml#EXIT-01 — contract must be ACTIVE.
        if contract.status != str(ContractStatus.ACTIVE):
            # 422 is fixed by algorithm.md; api.yaml lists no code for this
            # branch, and inventing one is forbidden (blockers.md#B-4).
            raise UndefinedErrorCode(
                "Exit may only be initiated on an ACTIVE contract.",
                http_status=422,
                blocker="B-4",
                contract_status=contract.status,
            )

        # algorithm.md step 2 / rules.yaml#EXIT-01, edges.yaml#X-001.
        workflows = WorkflowRepository(self._session)
        existing = await workflows.get_by_contract(command.contract_id)
        if existing is not None:
            raise ExitAlreadyInProgress(existing.id)

        # algorithm.md step 3 / rules.yaml#EXIT-02, edges.yaml#X-007.
        today = clock_module.today_dubai(self._clock)
        if command.move_out_date < today:
            raise MoveOutDateInPast(
                "move_out_date is before today in Asia/Dubai.",
                details={
                    "move_out_date": command.move_out_date.isoformat(),
                    "today_asia_dubai": today.isoformat(),
                },
            )
        reason = validate_reason(command.reason, self._reasons)
        if len(command.documents) < 1:
            raise DocumentsRequired("At least one document is required.")

        # algorithm.md step 4 / rules.yaml#EXIT-03 — one transaction.
        workflow_id = await workflows.next_workflow_id(today)
        workflow = ExitWorkflow(
            id=workflow_id,
            contract_id=contract.id,
            property_id=contract.property_id,
            tenant_id=contract.tenant_id,
            owner_id=contract.owner_id,
            status=str(State.INITIATED),
            move_out_date=command.move_out_date,
            reason=reason,
            documents=list(command.documents),
            # Snapshot: a later change to the contract must not move the refund
            # arithmetic of an exit already under way (rules.yaml#EXIT-07).
            security_deposit_minor=contract.security_deposit_minor,
        )
        workflows.add(workflow)

        # rules.yaml#EXIT-10 — the workflow's first state is a state change too.
        AuditRepository(self._session).append(
            workflow_id=workflow_id,
            actor_type=actor.role,
            actor_id=actor.subject_id,
            from_state=None,
            to_state=State.INITIATED,
            rule_id="EXIT-02",
            metadata={
                "contract_id": str(contract.id),
                "move_out_date": command.move_out_date.isoformat(),
                "reason": reason,
                "document_count": len(command.documents),
            },
        )

        # states.yaml: INITIATED -> DOCS_SUBMITTED, actor tenant,
        # requires [move_out_date, reason, documents], rule EXIT-02 — all three
        # were validated above.
        await apply_transition(
            self._session,
            workflow,
            State.DOCS_SUBMITTED,
            actor_type=actor.role,
            actor_id=actor.subject_id,
            metadata={"document_count": len(command.documents)},
        )

        # rules.yaml#EXIT-03 — exit lock in the same transaction as the insert.
        await PropertyRepository(self._session).acquire_exit_lock(contract.property_id, workflow_id)

        # rules.yaml#EXIT-04 — queued now, dispatched after commit.
        event = OutboxRepository(self._session).enqueue(
            topic=self._settings.kafka_events_topic,
            event_type=EventType.EXIT_INITIATED_OWNER_NOTIFICATION,
            event_key=workflow_id,
            payload={
                "event_type": str(EventType.EXIT_INITIATED_OWNER_NOTIFICATION),
                "workflow_id": workflow_id,
                "contract_id": str(contract.id),
                "property_id": str(contract.property_id),
                "owner_id": str(contract.owner_id),
                "tenant_id": str(contract.tenant_id),
                "move_out_date": command.move_out_date.isoformat(),
                "reason": reason,
                "occurred_at": clock_module.now_utc(self._clock).isoformat(),
            },
            available_at=clock_module.now_utc(self._clock),
        )

        try:
            await self._session.flush()
        except IntegrityError as exc:
            # edges.yaml#X-001 — two initiations racing on one contract. The
            # loser is told about the winner; a second workflow is never created.
            if db_errors.violates(exc, "uq_exit_workflows_contract"):
                await self._session.rollback()
                winner = await WorkflowRepository(self._session).get_by_contract(
                    command.contract_id
                )
                if winner is not None:
                    raise ExitAlreadyInProgress(winner.id) from exc
            raise

        logger.info(
            "exit workflow %s initiated for contract %s by tenant %s",
            workflow_id,
            contract.id,
            actor.subject_id,
        )
        return InitiationResult(
            workflow_id=workflow_id,
            persisted_status=State.DOCS_SUBMITTED,
            notification_event_id=event.id,
        )
