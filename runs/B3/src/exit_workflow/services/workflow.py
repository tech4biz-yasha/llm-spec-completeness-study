"""The exit workflow itself: initiation through completion.

Follows algorithm.md step by step. Each branch cites the rule it implements.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ..clock import UTC, Clock, business_today
from ..config import Settings
from ..db.models import Contract, ExitWorkflow, NocDocument, Payment, Property
from ..db.session import transaction
from ..domain.states import state_machine
from ..enums import (
    Actor,
    AdminTaskType,
    ContractStatus,
    PaymentStatus,
    PaymentType,
    WorkflowState,
)
from ..errors import (
    ContractNotActive,
    DocumentsRequired,
    ExitAlreadyInProgress,
    MoveOutDateInPast,
    NotAuthorized,
    PaymentPendingError,
    ReasonInvalid,
    SpecUnresolved,
    WorkflowNotFound,
    WrongState,
)
from ..money import CURRENCY, refund_minor, to_major, to_minor
from ..ports.events import EventPublisher
from ..ports.payments import GatewayResult, PaymentGateway
from ..ports.reference import ExitReasonReference
from ..ports.renderer import NocContext, NocRenderer
from ..ports.storage import ObjectStorage
from . import outbox
from .admin import open_admin_task
from .identity import Principal
from .ids import new_noc_id, new_payment_id, next_workflow_id
from .transitions import apply_transition, load_for_update, record_audit, state_history

logger = logging.getLogger(__name__)


def _violates(exc: IntegrityError, constraint: str) -> bool:
    """True when the IntegrityError is this constraint, not some other one."""
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    if diagnostic is not None and diagnostic.constraint_name:
        return diagnostic.constraint_name == constraint
    return constraint in str(exc)


@dataclass(frozen=True, slots=True)
class InitiationResult:
    workflow_id: str
    status: WorkflowState
    outbox_event_id: str


@dataclass(frozen=True, slots=True)
class _RefundHandle:
    """What the settlement transaction hands to the gateway call."""

    payment_id: str
    idempotency_key: str
    refund_amount: Decimal
    beneficiary_id: str


@dataclass(frozen=True, slots=True)
class SettlementResult:
    workflow_id: str
    refund_amount: Decimal
    payment_id: str
    status: WorkflowState


class ExitWorkflowService:
    """Every write in this module goes through here.

    Ports are constructor arguments so that nothing the kit leaves open (gateway,
    storage, NOC template, event bus, exit-reason reference data) is decided inside the
    workflow logic.
    """

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        settings: Settings,
        clock: Clock,
        reasons: ExitReasonReference,
        gateway: PaymentGateway,
        storage: ObjectStorage,
        renderer: NocRenderer,
        publisher: EventPublisher,
    ) -> None:
        self._sessions = session_factory
        self._settings = settings
        self._clock = clock
        self._reasons = reasons
        self._gateway = gateway
        self._storage = storage
        self._renderer = renderer
        self._publisher = publisher

    # ------------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        return self._clock.now_utc().astimezone(UTC)

    def _today(self) -> date:
        # edges.yaml#X-007 — Dubai calendar day, decision D-001.
        return business_today(self._clock)

    # ------------------------------------------------- algorithm.md #1-#5 (initiate)

    def initiate(
        self,
        *,
        principal: Principal,
        contract_id: str,
        move_out_date: date,
        reason: str,
        documents: Sequence[Any],
    ) -> InitiationResult:
        """POST /exit-workflows. api.yaml authz: tenant, own active contract only."""
        principal.require(Actor.TENANT)
        try:
            return self._initiate_in_one_transaction(
                principal=principal,
                contract_id=contract_id,
                move_out_date=move_out_date,
                reason=reason,
                documents=documents,
            )
        except IntegrityError as exc:
            # edges.yaml#X-001 under a race. The UNIQUE on contract_id is the real
            # guarantee that there is never a second workflow; the pre-check above only
            # gives the common case a clean error.
            if _violates(exc, "uq_exit_workflows_contract_id"):
                raise self._already_in_progress(contract_id) from exc
            raise

    def _initiate_in_one_transaction(
        self,
        *,
        principal: Principal,
        contract_id: str,
        move_out_date: date,
        reason: str,
        documents: Sequence[Any],
    ) -> InitiationResult:
        now = self._now()
        today = self._today()

        with transaction(self._sessions) as session:
            contract = (
                session.execute(select(Contract).where(Contract.id == contract_id))
                .unique()
                .scalar_one_or_none()
            )
            if contract is None:
                raise WorkflowNotFound(f"contract {contract_id} not found", contract_id=contract_id)

            # api.yaml: "tenant, own active contract only"
            if contract.tenant_id != principal.user_id:
                raise NotAuthorized(
                    "contract does not belong to the calling tenant", contract_id=contract_id
                )

            # algorithm.md#1 — assert status == ACTIVE, else 422. rules.yaml#EXIT-01
            if contract.status != ContractStatus.ACTIVE:
                raise ContractNotActive(
                    f"contract {contract_id} is {contract.status}, not ACTIVE",
                    contract_id=contract_id,
                    contract_status=contract.status,
                )

            # algorithm.md#2, rules.yaml#EXIT-01, edges.yaml#X-001
            existing = session.execute(
                select(ExitWorkflow.id).where(ExitWorkflow.contract_id == contract_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise ExitAlreadyInProgress(
                    f"an exit workflow already exists for contract {contract_id}",
                    workflow_id=existing,
                    contract_id=contract_id,
                )

            # algorithm.md#3, rules.yaml#EXIT-02. Validation order follows api.yaml's
            # 422 listing: MOVE_OUT_DATE_IN_PAST | REASON_INVALID | DOCUMENTS_REQUIRED.
            if move_out_date < today:
                # edges.yaml#X-007 — compared on the Asia/Dubai calendar.
                raise MoveOutDateInPast(
                    f"move_out_date {move_out_date.isoformat()} is before "
                    f"{today.isoformat()} in Asia/Dubai",
                    move_out_date=move_out_date.isoformat(),
                    today_asia_dubai=today.isoformat(),
                )
            if not self._reasons.is_valid(reason):
                # rules.yaml#EXIT-02 — reason must come from the reference list.
                raise ReasonInvalid(
                    f"reason {reason!r} is not in the reference list", reason=reason
                )
            if len(documents) < 1:
                # rules.yaml#EXIT-02 — at least one document.
                raise DocumentsRequired("at least one document is required")

            # algorithm.md#4, rules.yaml#EXIT-03: workflow insert, exit lock and audit
            # in ONE transaction.
            property_row = session.execute(
                select(Property).where(Property.id == contract.property_id).with_for_update()
            ).scalar_one()

            workflow_id = next_workflow_id(session, today)
            workflow = ExitWorkflow(
                id=workflow_id,
                contract_id=contract.id,
                property_id=contract.property_id,
                tenant_id=contract.tenant_id,
                owner_id=contract.owner_id,
                status=str(WorkflowState.INITIATED),
                move_out_date=move_out_date,
                reason=reason,
                documents=list(documents),
                security_deposit_minor=contract.security_deposit_minor,
                created_at=now,
                updated_at=now,
            )
            session.add(workflow)
            session.flush()

            # rules.yaml#EXIT-10 — the creation is itself a state change.
            record_audit(
                session,
                workflow_id=workflow.id,
                actor=Actor.TENANT,
                actor_id=principal.user_id,
                from_state=None,
                to_state=WorkflowState.INITIATED,
                rule_id="EXIT-01",
                metadata={"contract_id": contract.id, "reason": reason},
                occurred_at=now,
            )

            # states.yaml INITIATED -> DOCS_SUBMITTED (tenant, requires move_out_date,
            # reason, documents). rules.yaml#EXIT-02
            apply_transition(
                session,
                workflow,
                to_state=WorkflowState.DOCS_SUBMITTED,
                actor=Actor.TENANT,
                actor_id=principal.user_id,
                occurred_at=now,
                provided=("move_out_date", "reason", "documents"),
                metadata={
                    "move_out_date": move_out_date.isoformat(),
                    "reason": reason,
                    "document_count": len(documents),
                },
            )

            # rules.yaml#EXIT-03 — same transaction as the workflow insert.
            property_row.exit_lock = True
            property_row.exit_lock_workflow_id = workflow.id
            property_row.exit_lock_set_at = now

            # rules.yaml#EXIT-04 — durable enqueue now, dispatch strictly after commit.
            event = outbox.enqueue(
                session,
                topic=self._settings.owner_notification_topic,
                key=workflow.id,
                payload={
                    "event": "exit_workflow.owner_notification",
                    "workflow_id": workflow.id,
                    "contract_id": contract.id,
                    "property_id": contract.property_id,
                    "owner_id": contract.owner_id,
                    "tenant_id": contract.tenant_id,
                    "move_out_date": move_out_date.isoformat(),
                    "reason": reason,
                    "occurred_at": now.isoformat(),
                },
                workflow_id=workflow.id,
                occurred_at=now,
            )
            session.flush()

            return InitiationResult(
                workflow_id=workflow.id,
                status=workflow.state,
                outbox_event_id=event.id,
            )

    def _already_in_progress(self, contract_id: str) -> ExitAlreadyInProgress:
        with transaction(self._sessions) as session:
            existing = session.execute(
                select(ExitWorkflow.id).where(ExitWorkflow.contract_id == contract_id)
            ).scalar_one_or_none()
        return ExitAlreadyInProgress(
            f"an exit workflow already exists for contract {contract_id}",
            workflow_id=existing,
            contract_id=contract_id,
        )

    # ------------------------------------------------- algorithm.md #5 (notify owner)

    def notify_owner(self, workflow_id: str, event_id: str | None = None) -> WorkflowState:
        """states.yaml DOCS_SUBMITTED -> OWNER_NOTIFIED, side_effect notify_owner.

        rules.yaml#EXIT-04 / edges.yaml#X-002: this runs AFTER the initiation transaction
        has committed, and a dispatch failure never rolls the workflow back. The workflow
        advances because the event is durably queued; delivery is the outbox's problem
        from here, through 5 attempts and then dead-letter. See blockers.md#B-3 for the
        reading of X-002 this implements.
        """
        now = self._now()
        with transaction(self._sessions) as session:
            workflow = load_for_update(session, workflow_id)
            if workflow.state is WorkflowState.DOCS_SUBMITTED:
                apply_transition(
                    session,
                    workflow,
                    to_state=WorkflowState.OWNER_NOTIFIED,
                    actor=Actor.SYSTEM,
                    actor_id=None,
                    occurred_at=now,
                    metadata={"side_effect": "notify_owner", "event_id": event_id},
                )
            state = workflow.state

        # Dispatch outside the workflow transaction. Its own transaction records only the
        # outbox row's fate.
        with transaction(self._sessions) as session:
            if event_id is not None:
                outbox.dispatch_event(
                    session,
                    event_id,
                    publisher=self._publisher,
                    settings=self._settings,
                    now=now,
                )
            else:
                outbox.dispatch_due(
                    session,
                    publisher=self._publisher,
                    settings=self._settings,
                    now=now,
                )
        return state

    def dispatch_pending_notifications(self, limit: int = 100) -> outbox.DispatchOutcome:
        """Retry sweep for rules.yaml#EXIT-04. Run on a schedule."""
        now = self._now()
        with transaction(self._sessions) as session:
            return outbox.dispatch_due(
                session,
                publisher=self._publisher,
                settings=self._settings,
                now=now,
                limit=limit,
            )

    def recover_unnotified(self, limit: int = 100) -> list[str]:
        """Advance workflows that committed but whose notify step never ran.

        Covers a process death between the initiation commit and ``notify_owner``.
        """
        now = self._now()
        advanced: list[str] = []
        with transaction(self._sessions) as session:
            stuck = (
                session.execute(
                    select(ExitWorkflow)
                    .where(ExitWorkflow.status == str(WorkflowState.DOCS_SUBMITTED))
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                .unique()
                .scalars()
                .all()
            )
            for workflow in stuck:
                apply_transition(
                    session,
                    workflow,
                    to_state=WorkflowState.OWNER_NOTIFIED,
                    actor=Actor.SYSTEM,
                    actor_id=None,
                    occurred_at=now,
                    metadata={"side_effect": "notify_owner", "recovered": True},
                )
                advanced.append(workflow.id)
        if advanced:
            self.dispatch_pending_notifications()
        return advanced

    # ------------------------------------------------- algorithm.md #6 (schedule)

    def schedule_inspection(self, workflow_id: str, *, principal: Principal) -> WorkflowState:
        """POST /exit-workflows/{id}/schedule-inspection. api.yaml authz: owner."""
        principal.require(Actor.OWNER)
        now = self._now()
        with transaction(self._sessions) as session:
            workflow = load_for_update(session, workflow_id)
            self._assert_owner(workflow, principal)
            # states.yaml OWNER_NOTIFIED -> INSPECTION_SCHEDULED. rules.yaml#EXIT-05
            apply_transition(
                session,
                workflow,
                to_state=WorkflowState.INSPECTION_SCHEDULED,
                actor=Actor.OWNER,
                actor_id=principal.user_id,
                occurred_at=now,
            )
            return workflow.state

    # ------------------------------------------------- algorithm.md #7 (inspection)

    def submit_inspection_report(
        self,
        workflow_id: str,
        *,
        principal: Principal,
        damage_amount: Decimal,
        photos: Sequence[Any],
    ) -> WorkflowState:
        """POST /exit-workflows/{id}/inspection-report. api.yaml authz: inspection_agency.

        rules.yaml#EXIT-06 — the agency enters the assessment with photos; the owner
        confirms it later. api.yaml declares no error code for a photo-count or amount
        rule, and the kit states neither, so neither is invented here beyond the
        non-negative money invariant carried by the request schema. See blockers.md#B-5.
        """
        principal.require(Actor.INSPECTOR)
        now = self._now()
        damage_minor = to_minor(damage_amount)
        with transaction(self._sessions) as session:
            workflow = load_for_update(session, workflow_id)
            # states.yaml INSPECTION_SCHEDULED -> INSPECTION_DONE, actor inspector.
            apply_transition(
                session,
                workflow,
                to_state=WorkflowState.INSPECTION_DONE,
                actor=Actor.INSPECTOR,
                actor_id=principal.user_id,
                occurred_at=now,
                metadata={
                    "damage_amount": str(to_major(damage_minor)),
                    "currency": CURRENCY,
                    "photo_count": len(photos),
                },
            )
            workflow.damage_amount_minor = damage_minor
            workflow.damage_photos = list(photos)
            workflow.inspection_reported_at = now
            return workflow.state

    # ------------------------------------------------- algorithm.md #8 (confirm)

    def confirm_damage(self, workflow_id: str, *, principal: Principal) -> WorkflowState:
        """POST /exit-workflows/{id}/confirm-damage. api.yaml authz: owner.

        rules.yaml#EXIT-06 — owner confirmation is required before settlement. The
        forbidden entry ``INSPECTION_DONE -> REFUND_PROCESSED`` is the same rule expressed
        in states.yaml, and it is enforced there.
        """
        principal.require(Actor.OWNER)
        now = self._now()
        with transaction(self._sessions) as session:
            workflow = load_for_update(session, workflow_id)
            self._assert_owner(workflow, principal)
            apply_transition(
                session,
                workflow,
                to_state=WorkflowState.DAMAGE_CONFIRMED,
                actor=Actor.OWNER,
                actor_id=principal.user_id,
                occurred_at=now,
                metadata={
                    "damage_amount": (
                        str(to_major(workflow.damage_amount_minor))
                        if workflow.damage_amount_minor is not None
                        else None
                    )
                },
            )
            workflow.damage_confirmed_at = now
            return workflow.state

    def dispute_damage(self, workflow_id: str, *, principal: Principal) -> None:
        """rules.yaml#EXIT-06: "Owner may dispute once; a dispute routes to admin review."

        BLOCKED. states.yaml defines no disputed state and no transition for it, api.yaml
        defines no dispute endpoint or error code, and nothing says what the workflow's
        state is while admin review is pending or where it returns to afterwards. Raising
        rather than guessing, per AGENTS.md. See blockers.md#B-1.
        """
        raise SpecUnresolved(
            "B-1",
            "owner dispute (rules.yaml#EXIT-06) has no state in states.yaml, no "
            "transition, and no endpoint in api.yaml",
            workflow_id=workflow_id,
        )

    # ------------------------------------------- algorithm.md #9-#13 (settle to complete)

    def settle(self, workflow_id: str, *, principal: Principal) -> SettlementResult:
        """POST /exit-workflows/{id}/settle. api.yaml authz: system|owner.

        Drives algorithm.md steps 9 through 13: the R8 branch, the refund, the wait for
        SUCCEEDED, NOC issuance, then COMPLETE with the lock released.
        """
        principal.require(Actor.SYSTEM, Actor.OWNER)

        handle = self._create_or_get_refund(workflow_id, principal)

        # algorithm.md#11 — external call, deliberately outside any open transaction.
        gateway_result = self._gateway.initiate_refund(
            idempotency_key=handle.idempotency_key,
            amount_minor=to_minor(handle.refund_amount),
            currency=CURRENCY,
            beneficiary_id=handle.beneficiary_id,
            metadata={"workflow_id": workflow_id, "type": str(PaymentType.DEPOSIT_REFUND)},
        )
        self._record_gateway_result(handle.payment_id, gateway_result)

        if gateway_result.status is not PaymentStatus.SUCCEEDED:
            # algorithm.md#11, edges.yaml#X-004 — PENDING or FAILED: hold, never proceed.
            # The workflow stays at REFUND_PROCESSED and no NOC is generated. api.yaml
            # gives PAYMENT_PENDING as the 409 for settle; it is the only code covering
            # "the refund has not settled". The recovery path for a terminally FAILED
            # refund is unspecified — blockers.md#B-12.
            raise PaymentPendingError(
                f"refund payment is {gateway_result.status}, not SUCCEEDED; "
                "NOC issuance is refused",
                workflow_id=workflow_id,
                payment_id=handle.payment_id,
                payment_status=str(gateway_result.status),
            )

        status = self._issue_noc_and_complete(
            workflow_id, handle.payment_id, handle.refund_amount, principal
        )
        return SettlementResult(
            workflow_id=workflow_id,
            refund_amount=handle.refund_amount,
            payment_id=handle.payment_id,
            status=status,
        )

    def _create_or_get_refund(self, workflow_id: str, principal: Principal) -> _RefundHandle:
        """algorithm.md#9-#10 in one transaction, holding the workflow row lock.

        edges.yaml#X-005: the row lock plus the UNIQUE idempotency key mean two racing
        settlements produce exactly one payment; the loser returns the existing one.
        """
        now = self._now()
        with transaction(self._sessions) as session:
            workflow = load_for_update(session, workflow_id)

            if workflow.state in (
                WorkflowState.REFUND_PROCESSED,
                WorkflowState.NOC_ISSUED,
                WorkflowState.COMPLETE,
            ):
                # Re-entrant settle: the payment already exists. edges.yaml#X-005
                payment = session.execute(
                    select(Payment).where(Payment.idempotency_key == workflow_id)
                ).scalar_one()
                return _RefundHandle(
                    payment_id=payment.id,
                    idempotency_key=workflow_id,
                    refund_amount=to_major(payment.amount_minor),
                    beneficiary_id=workflow.tenant_id,
                )

            if workflow.state is not WorkflowState.DAMAGE_CONFIRMED:
                # Let states.yaml produce the error, so the forbidden list is what refuses
                # e.g. INSPECTION_DONE -> REFUND_PROCESSED (rules.yaml#EXIT-06). This
                # always raises: DAMAGE_CONFIRMED is the only origin for REFUND_PROCESSED.
                state_machine().validate(
                    from_state=workflow.state,
                    to_state=WorkflowState.REFUND_PROCESSED,
                    actor=Actor.SYSTEM,
                    history=state_history(session, workflow.id) | {workflow.state},
                )
                raise WrongState(  # pragma: no cover - unreachable, states.yaml raises above
                    f"settlement requires DAMAGE_CONFIRMED, workflow is {workflow.status}",
                    workflow_id=workflow_id,
                    status=workflow.status,
                )

            deposit_minor = workflow.contract.security_deposit_minor
            damage_minor = workflow.damage_amount_minor
            if damage_minor is None:  # pragma: no cover - unreachable via the state machine
                raise WrongState(
                    "no confirmed damage amount on the workflow", workflow_id=workflow_id
                )

            # algorithm.md#9, rules.yaml#EXIT-07, edges.yaml#X-003 — BLOCKED on R8.
            if damage_minor > deposit_minor:
                raise SpecUnresolved(
                    "R8",
                    "confirmed damage exceeds the security deposit; rules.yaml#EXIT-07 "
                    "leaves this case open (risks.md#R8). No refund, no NOC, workflow "
                    "holds at DAMAGE_CONFIRMED.",
                    workflow_id=workflow_id,
                    confirmed_damage=str(to_major(damage_minor)),
                    security_deposit=str(to_major(deposit_minor)),
                )

            # algorithm.md#10, rules.yaml#EXIT-07 — Decimal, half-up, 2 dp.
            amount_minor = refund_minor(deposit_minor, damage_minor)

            payment = Payment(
                id=new_payment_id(),
                type=str(PaymentType.DEPOSIT_REFUND),
                idempotency_key=workflow_id,  # rules.yaml#EXIT-08
                workflow_id=workflow_id,
                amount_minor=amount_minor,
                currency=CURRENCY,
                status=str(PaymentStatus.PENDING),
                created_at=now,
                updated_at=now,
            )
            session.add(payment)
            session.flush()

            workflow.refund_amount_minor = amount_minor
            workflow.payment_id = payment.id
            # states.yaml DAMAGE_CONFIRMED -> REFUND_PROCESSED. rules.yaml#EXIT-07
            apply_transition(
                session,
                workflow,
                to_state=WorkflowState.REFUND_PROCESSED,
                actor=Actor.SYSTEM,
                actor_id=principal.user_id,
                occurred_at=now,
                metadata={
                    "payment_id": payment.id,
                    "refund_amount": str(to_major(amount_minor)),
                    "security_deposit": str(to_major(deposit_minor)),
                    "confirmed_damage": str(to_major(damage_minor)),
                    "initiated_by_role": str(principal.role),
                },
            )
            return _RefundHandle(
                payment_id=payment.id,
                idempotency_key=workflow_id,
                refund_amount=to_major(amount_minor),
                beneficiary_id=workflow.tenant_id,
            )

    def _record_gateway_result(self, payment_id: str, result: GatewayResult) -> None:
        now = self._now()
        with transaction(self._sessions) as session:
            payment = session.execute(
                select(Payment).where(Payment.id == payment_id).with_for_update()
            ).scalar_one()
            payment.status = str(result.status)
            payment.gateway_reference = result.reference
            payment.failure_reason = result.failure_reason
            payment.updated_at = now

    def _issue_noc_and_complete(
        self,
        workflow_id: str,
        payment_id: str,
        refund_amount: Decimal,
        principal: Principal,
    ) -> WorkflowState:
        """algorithm.md#12-#13, rules.yaml#EXIT-09.

        The workflow row lock is held across the render and the object-store write. That
        is deliberate: the NOC is immutable once issued, so two racing settlements must
        not both reach the store. edges.yaml#X-005 — one payment, one NOC, one COMPLETE.
        """
        now = self._now()

        with transaction(self._sessions) as session:
            workflow = load_for_update(session, workflow_id)
            if workflow.state is WorkflowState.COMPLETE:
                return workflow.state  # already finished; settle is re-entrant
            payment = session.execute(select(Payment).where(Payment.id == payment_id)).scalar_one()
            if payment.status != str(PaymentStatus.SUCCEEDED):
                # rules.yaml#EXIT-08, edges.yaml#X-004 — NOC only after SUCCEEDED.
                raise PaymentPendingError(
                    f"refund payment is {payment.status}, not SUCCEEDED",
                    workflow_id=workflow_id,
                    payment_id=payment_id,
                    payment_status=payment.status,
                )

            existing_noc = session.execute(
                select(NocDocument).where(NocDocument.workflow_id == workflow_id)
            ).scalar_one_or_none()
            object_key = f"noc/{workflow.id}.pdf"

            if existing_noc is not None:
                noc_id = existing_noc.id
            else:
                # algorithm.md#12 — generate the PDF, store it in the UAE bucket.
                noc_id = new_noc_id()
                document = self._renderer.render(
                    NocContext(
                        workflow_id=workflow.id,
                        contract_id=workflow.contract_id,
                        property_id=workflow.property_id,
                        tenant_id=workflow.tenant_id,
                        owner_id=workflow.owner_id,
                        move_out_date=workflow.move_out_date,
                        refund_amount=refund_amount,
                        currency=CURRENCY,
                        payment_id=payment.id,
                        payment_reference=payment.gateway_reference,
                        issued_at=now,
                    )
                )
                stored = self._storage.put_immutable(
                    bucket=self._settings.noc_bucket,
                    key=object_key,
                    body=document,
                    content_type="application/pdf",
                )
                session.add(
                    NocDocument(
                        id=noc_id,
                        workflow_id=workflow.id,
                        bucket=stored.bucket,
                        object_key=stored.key,
                        region=stored.region,
                        content_type="application/pdf",
                        size_bytes=stored.size_bytes,
                        sha256=stored.sha256,
                        issued_at=now,
                    )
                )
                session.flush()
            workflow.noc_document_id = noc_id

            if workflow.state is WorkflowState.REFUND_PROCESSED:
                # states.yaml REFUND_PROCESSED -> NOC_ISSUED. rules.yaml#EXIT-08, T13
                # order: refund first, then NOC. The forbidden entry "any -> NOC_ISSUED
                # without REFUND_PROCESSED" is checked inside apply_transition.
                apply_transition(
                    session,
                    workflow,
                    to_state=WorkflowState.NOC_ISSUED,
                    actor=Actor.SYSTEM,
                    actor_id=principal.user_id,
                    occurred_at=now,
                    metadata={
                        "noc_document_id": noc_id,
                        "bucket": self._settings.noc_bucket,
                        "object_key": object_key,
                        "region": self._settings.noc_bucket_region,
                    },
                )

            # rules.yaml#EXIT-09 / algorithm.md#13 — COMPLETE and the lock release are in
            # this one transaction, together with the audit row.
            apply_transition(
                session,
                workflow,
                to_state=WorkflowState.COMPLETE,
                actor=Actor.SYSTEM,
                actor_id=principal.user_id,
                occurred_at=now,
                metadata={"side_effect": "release_exit_lock"},
            )
            property_row = session.execute(
                select(Property).where(Property.id == workflow.property_id).with_for_update()
            ).scalar_one()
            property_row.exit_lock = False
            property_row.exit_lock_workflow_id = None
            property_row.exit_lock_set_at = None
            return workflow.state

    # ------------------------------------------------- algorithm.md #6 (stall sweep)

    def run_stall_sweep(self, limit: int = 500) -> list[str]:
        """rules.yaml#EXIT-05 — 30 days past move_out_date with no inspection completed.

        states.yaml allows STALLED from OWNER_NOTIFIED and from INSPECTION_SCHEDULED. The
        workflow does not auto-cancel; an admin task is opened.
        """
        now = self._now()
        cutoff = self._today() - timedelta(days=self._settings.inspection_window_days)
        stalled: list[str] = []
        with transaction(self._sessions) as session:
            candidates = (
                session.execute(
                    select(ExitWorkflow)
                    .where(
                        ExitWorkflow.status.in_(
                            [
                                str(WorkflowState.OWNER_NOTIFIED),
                                str(WorkflowState.INSPECTION_SCHEDULED),
                            ]
                        ),
                        ExitWorkflow.move_out_date < cutoff,
                    )
                    .order_by(ExitWorkflow.move_out_date)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
                .unique()
                .scalars()
                .all()
            )
            for workflow in candidates:
                apply_transition(
                    session,
                    workflow,
                    to_state=WorkflowState.STALLED,
                    actor=Actor.SYSTEM,
                    actor_id=None,
                    occurred_at=now,
                    metadata={
                        "when": "30_days_past_move_out",
                        "move_out_date": workflow.move_out_date.isoformat(),
                        "window_days": self._settings.inspection_window_days,
                    },
                )
                workflow.stalled_at = now
                open_admin_task(
                    session,
                    task_type=AdminTaskType.EXIT_WORKFLOW_STALLED,
                    workflow_id=workflow.id,
                    payload={
                        "move_out_date": workflow.move_out_date.isoformat(),
                        "previous_status": str(WorkflowState.STALLED),
                        "reason": "inspection not completed within "
                        f"{self._settings.inspection_window_days} days of move_out_date",
                    },
                    occurred_at=now,
                )
                stalled.append(workflow.id)
        return stalled

    # ------------------------------------------------------------------ reads

    def get(self, workflow_id: str, *, principal: Principal) -> ExitWorkflow:
        with transaction(self._sessions) as session:
            workflow = (
                session.execute(select(ExitWorkflow).where(ExitWorkflow.id == workflow_id))
                .unique()
                .scalar_one_or_none()
            )
            if workflow is None:
                raise WorkflowNotFound(
                    f"exit workflow {workflow_id} not found", workflow_id=workflow_id
                )
            if principal.role is Actor.TENANT and workflow.tenant_id != principal.user_id:
                raise NotAuthorized("workflow does not belong to the calling tenant")
            if principal.role is Actor.OWNER and workflow.owner_id != principal.user_id:
                raise NotAuthorized("workflow does not belong to the calling owner")
            return workflow

    @staticmethod
    def _assert_owner(workflow: ExitWorkflow, principal: Principal) -> None:
        """api.yaml ``authz: owner`` — the owner of this workflow's property."""
        if principal.role is Actor.OWNER and workflow.owner_id != principal.user_id:
            raise NotAuthorized(
                "workflow does not belong to the calling owner", workflow_id=workflow.id
            )
