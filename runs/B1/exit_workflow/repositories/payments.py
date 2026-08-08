"""Payment access (rules.yaml#EXIT-08)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.db.models import Payment
from exit_workflow.domain.enums import PaymentStatus, PaymentType
from exit_workflow.domain.money import CURRENCY


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_idempotency_key(self, key: str) -> Payment | None:
        return await self._session.scalar(select(Payment).where(Payment.idempotency_key == key))

    async def create_or_get(
        self,
        *,
        idempotency_key: str,
        payment_type: PaymentType,
        workflow_id: str,
        contract_id: uuid.UUID,
        payee_id: uuid.UUID,
        amount_minor: int,
    ) -> tuple[Payment, bool]:
        """Create the refund payment, or return the one that already exists.

        edges.yaml#X-005: "Idempotency key = workflow_id means one payment,
        second call returns existing." The uniqueness decision is made by
        PostgreSQL rather than by a read-then-write check in Python, so two
        settlement attempts racing on separate connections still produce exactly
        one payment.

        :returns: ``(payment, created)``.
        """
        statement = (
            pg_insert(Payment)
            .values(
                id=uuid.uuid4(),
                payment_type=str(payment_type),
                workflow_id=workflow_id,
                contract_id=contract_id,
                payee_id=payee_id,
                amount_minor=amount_minor,
                currency=CURRENCY,
                status=str(PaymentStatus.PENDING),
                idempotency_key=idempotency_key,
            )
            .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
            .returning(Payment.id)
        )
        created_id = await self._session.scalar(statement)
        if created_id is not None:
            payment = await self._session.get(Payment, created_id)
            assert payment is not None  # noqa: S101 - just inserted in this transaction
            return payment, True

        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is None:  # pragma: no cover - only reachable if the row vanished
            raise RuntimeError(
                f"payment with idempotency key {idempotency_key!r} conflicted on insert "
                "but could not be read back"
            )
        return existing, False
