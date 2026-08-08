"""NOC issuance and completion — algorithm.md steps 11-13.

    11. Await gateway SUCCEEDED. PENDING or FAILED -> hold, never proceed.
                                                            (EXIT-08, X-004)
    12. Generate NOC PDF, store UAE bucket, immutable, link  (EXIT-09)
    13. IN ONE TRANSACTION: status = COMPLETE, release
        property.exitLock, audit row                         (EXIT-09)
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..clock import DUBAI, Clock
from ..config import Settings
from ..db.models import ExitWorkflow, NocDocument, Payment, PaymentStatus, Property
from ..db.session import transaction
from ..domain.states import State
from ..errors import PaymentPending, WrongState
from ..money import CURRENCY, from_minor
from ..ports import NocContext, NocRenderer, ObjectStore
from .transitions import SYSTEM_PRINCIPAL, TransitionService


class NocService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        transitions: TransitionService,
        renderer: NocRenderer,
        object_store: ObjectStore,
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._transitions = transitions
        self._renderer = renderer
        self._object_store = object_store
        self._settings = settings

    async def issue_and_complete(self, workflow_id: str) -> ExitWorkflow:
        """Steps 12-13. Only reachable from REFUND_PROCESSED.

        states.yaml forbids `any -> NOC_ISSUED without REFUND_PROCESSED` (T13
        order, rules.yaml#EXIT-08); the state machine enforces that, and the
        payment status is re-checked here so no caller can reach NOC issuance
        with an unsettled refund (edges.yaml#X-004).

        The whole step runs under the workflow row lock, PDF rendering and
        object write included. That holds a database transaction across a
        storage call, which is deliberate: it is one short transaction on one
        row, and it is what makes a concurrent second attempt wait and then
        observe COMPLETE instead of writing a second NOC object for a document
        the spec calls immutable.
        """
        async with transaction(self._session_factory) as session:
            workflow = (
                await session.execute(
                    select(ExitWorkflow).where(ExitWorkflow.id == workflow_id).with_for_update()
                )
            ).scalar_one()

            if workflow.status in (State.NOC_ISSUED, State.COMPLETE):
                # rules.yaml#EXIT-09 — immutable once issued. Nothing to redo.
                return workflow

            if workflow.status is not State.REFUND_PROCESSED:
                raise WrongState(
                    f"NOC requires REFUND_PROCESSED, workflow is {workflow.status}",
                    from_state=workflow.status,
                    to_state=State.NOC_ISSUED,
                )

            payment = (
                await session.execute(select(Payment).where(Payment.workflow_id == workflow.id))
            ).scalar_one_or_none()
            # edges.yaml#X-004 — NOC only after SUCCEEDED. Refuse otherwise.
            if payment is None or payment.status is not PaymentStatus.SUCCEEDED:
                raise PaymentPending(
                    "refund payment is not SUCCEEDED; NOC cannot be issued",
                    details={
                        "payment_status": payment.status.value if payment else None,
                        "workflow_id": workflow.id,
                    },
                )

            context = NocContext(
                workflow_id=workflow.id,
                contract_id=str(workflow.contract_id),
                property_id=str(workflow.property_id),
                tenant_id=str(workflow.tenant_id),
                owner_id=str(workflow.owner_id),
                move_out_date=workflow.move_out_date,
                security_deposit=from_minor(workflow.security_deposit_minor),
                confirmed_damage=from_minor(workflow.confirmed_damage_minor or 0),
                refund_amount=from_minor(workflow.refund_amount_minor or 0),
                currency=CURRENCY,
                payment_reference=payment.gateway_reference or str(payment.id),
                # D-001 — the date on the document is the Dubai date.
                issued_at_dubai=self._clock.now_utc().astimezone(DUBAI).isoformat(),
            )

            # rules.yaml#EXIT-09 — PDF, UAE region bucket, immutable.
            pdf = self._renderer.render(context)
            key = f"{self._settings.noc_key_prefix}/{workflow.id}/noc.pdf"
            stored = await self._object_store.put_immutable(key, pdf, "application/pdf")

            document = NocDocument(
                id=uuid.uuid4(),
                workflow_id=workflow.id,
                bucket=stored.bucket,
                object_key=stored.key,
                region=stored.region,
                content_sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                issued_at=self._clock.now_utc(),
            )
            session.add(document)
            workflow.noc_document_id = document.id

            await self._transitions.apply(
                session,
                workflow,
                State.NOC_ISSUED,
                SYSTEM_PRINCIPAL,
                metadata={
                    "noc_document_id": str(document.id),
                    "bucket": stored.bucket,
                    "object_key": stored.key,
                    "region": stored.region,
                    "sha256": stored.sha256,
                },
            )

            # rules.yaml#EXIT-09 — COMPLETE is set and property.exitLock released
            # in ONE transaction after NOC issuance. This is that transaction:
            # the NOC row, both state changes, their audit rows and the lock
            # release commit together or not at all.
            property_row = (
                await session.execute(
                    select(Property).where(Property.id == workflow.property_id).with_for_update()
                )
            ).scalar_one()
            property_row.exit_lock = False
            workflow.completed_at = self._clock.now_utc()

            await self._transitions.apply(
                session,
                workflow,
                State.COMPLETE,
                SYSTEM_PRINCIPAL,
                metadata={"exit_lock_released": True, "property_id": str(workflow.property_id)},
            )
            return workflow
