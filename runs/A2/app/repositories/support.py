"""Repositories for the supporting entities."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.domain.enums import DisputeStatus, OutboxStatus
from app.models.audit import AuditLogEntry
from app.models.document import ExitDocument
from app.models.idempotency import IdempotencyRecord
from app.models.inspection import DamageItem, Inspection, InspectionSlot
from app.models.noc import ExitNoc
from app.models.outbox import OutboxMessage
from app.models.settlement import Settlement


class DocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, document: ExitDocument) -> None:
        self._session.add(document)

    async def get(self, document_id: uuid.UUID) -> ExitDocument | None:
        return await self._session.get(ExitDocument, document_id)

    async def require_in_workflow(
        self, document_id: uuid.UUID, workflow_id: uuid.UUID
    ) -> ExitDocument:
        document = await self.get(document_id)
        if document is None or document.workflow_id != workflow_id:
            raise NotFoundError(
                "Document not found on this exit workflow.",
                details={"document_id": str(document_id)},
            )
        return document

    async def list_active(self, workflow_id: uuid.UUID) -> list[ExitDocument]:
        stmt = (
            select(ExitDocument)
            .where(
                ExitDocument.workflow_id == workflow_id,
                ExitDocument.deleted_at.is_(None),
            )
            .order_by(ExitDocument.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_active(self, workflow_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            ExitDocument.workflow_id == workflow_id, ExitDocument.deleted_at.is_(None)
        )
        return int((await self._session.execute(stmt)).scalar_one())


class InspectionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, inspection: Inspection) -> None:
        self._session.add(inspection)

    async def get_for_workflow(self, workflow_id: uuid.UUID) -> Inspection | None:
        stmt = select(Inspection).where(Inspection.workflow_id == workflow_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def require_for_workflow(self, workflow_id: uuid.UUID) -> Inspection:
        inspection = await self.get_for_workflow(workflow_id)
        if inspection is None:
            raise NotFoundError(
                "No inspection has been requested for this exit workflow.",
                details={"workflow_id": str(workflow_id)},
            )
        return inspection

    async def require_slot(self, inspection_id: uuid.UUID, slot_id: uuid.UUID) -> InspectionSlot:
        stmt = select(InspectionSlot).where(
            InspectionSlot.id == slot_id, InspectionSlot.inspection_id == inspection_id
        )
        slot = (await self._session.execute(stmt)).scalar_one_or_none()
        if slot is None:
            raise NotFoundError(
                "Inspection slot not found.", details={"slot_id": str(slot_id)}
            )
        return slot

    async def require_damage_item(
        self, inspection_id: uuid.UUID, item_id: uuid.UUID
    ) -> DamageItem:
        stmt = select(DamageItem).where(
            DamageItem.id == item_id, DamageItem.inspection_id == inspection_id
        )
        item = (await self._session.execute(stmt)).scalar_one_or_none()
        if item is None:
            raise NotFoundError(
                "Damage item not found.", details={"damage_item_id": str(item_id)}
            )
        return item

    async def count_open_disputes(self, inspection_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            DamageItem.inspection_id == inspection_id,
            DamageItem.dispute_status == DisputeStatus.RAISED.value,
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def clear_slot_selection(self, inspection_id: uuid.UUID) -> None:
        await self._session.execute(
            update(InspectionSlot)
            .where(
                InspectionSlot.inspection_id == inspection_id,
                InspectionSlot.is_selected.is_(True),
            )
            .values(is_selected=False, selected_at=None, selected_by=None)
        )

    async def delete_unselected_slots(self, inspection_id: uuid.UUID) -> None:
        await self._session.execute(
            delete(InspectionSlot).where(
                InspectionSlot.inspection_id == inspection_id,
                InspectionSlot.is_selected.is_(False),
            )
        )


class SettlementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, settlement: Settlement) -> None:
        self._session.add(settlement)

    async def get_for_workflow(self, workflow_id: uuid.UUID) -> Settlement | None:
        stmt = select(Settlement).where(Settlement.workflow_id == workflow_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_for_workflow_for_update(self, workflow_id: uuid.UUID) -> Settlement | None:
        stmt = (
            select(Settlement)
            .where(Settlement.workflow_id == workflow_id)
            .with_for_update(of=Settlement)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def require_for_workflow(self, workflow_id: uuid.UUID) -> Settlement:
        settlement = await self.get_for_workflow(workflow_id)
        if settlement is None:
            raise NotFoundError(
                "No settlement exists for this exit workflow yet.",
                details={"workflow_id": str(workflow_id)},
            )
        return settlement

    async def find_by_payment_reference(self, reference: str) -> Settlement | None:
        """Resolve a provider webhook back to its settlement, under a row lock."""
        stmt = (
            select(Settlement)
            .where(Settlement.payment_reference == reference)
            .with_for_update(of=Settlement)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class NocRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, noc: ExitNoc) -> None:
        self._session.add(noc)

    async def get_for_workflow(self, workflow_id: uuid.UUID) -> ExitNoc | None:
        stmt = select(ExitNoc).where(ExitNoc.workflow_id == workflow_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def require_for_workflow(self, workflow_id: uuid.UUID) -> ExitNoc:
        noc = await self.get_for_workflow(workflow_id)
        if noc is None:
            raise NotFoundError(
                "No NOC has been issued for this exit workflow yet.",
                details={"workflow_id": str(workflow_id)},
            )
        return noc

    async def get_by_verification_code(self, code: str) -> ExitNoc | None:
        stmt = select(ExitNoc).where(ExitNoc.verification_code == code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def next_number(self, year: int) -> str:
        value = (
            await self._session.execute(select(func.nextval("exit_workflow_noc_number_seq")))
        ).scalar_one()
        return f"NOC-{year}-{int(value):06d}"

    async def record_download(self, noc_id: uuid.UUID, at: datetime) -> None:
        """Bump the download counter without loading or locking the row."""
        await self._session.execute(
            update(ExitNoc)
            .where(ExitNoc.id == noc_id)
            .values(
                download_count=ExitNoc.download_count + 1,
                last_downloaded_at=at,
                first_downloaded_at=func.coalesce(ExitNoc.first_downloaded_at, at),
            )
        )


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, entry: AuditLogEntry) -> None:
        self._session.add(entry)

    async def list_for_workflow(
        self, workflow_id: uuid.UUID, *, limit: int = 200, offset: int = 0
    ) -> list[AuditLogEntry]:
        stmt = (
            select(AuditLogEntry)
            .where(AuditLogEntry.workflow_id == workflow_id)
            .order_by(AuditLogEntry.occurred_at.desc(), AuditLogEntry.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, message: OutboxMessage) -> None:
        self._session.add(message)

    async def claim_batch(self, *, now: datetime, limit: int) -> list[OutboxMessage]:
        """Claim publishable messages.

        ``FOR UPDATE SKIP LOCKED`` lets N dispatcher replicas drain the table in
        parallel: each claims a disjoint set, and a crash releases the lock so another
        replica picks the rows up on its next pass.
        """
        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.status.in_(
                    [OutboxStatus.PENDING.value, OutboxStatus.FAILED.value]
                ),
                OutboxMessage.available_at <= now,
            )
            .order_by(OutboxMessage.available_at, OutboxMessage.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def purge_published(self, *, before: datetime) -> int:
        result = await self._session.execute(
            delete(OutboxMessage).where(
                OutboxMessage.status == OutboxStatus.PUBLISHED.value,
                OutboxMessage.published_at < before,
            )
        )
        return int(result.rowcount or 0)


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(self, key: str, endpoint: str) -> IdempotencyRecord | None:
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.idempotency_key == key,
            IdempotencyRecord.endpoint == endpoint,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    def add(self, record: IdempotencyRecord) -> None:
        self._session.add(record)

    async def purge_expired(self, *, now: datetime) -> int:
        result = await self._session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def default_expiry(now: datetime, *, hours: int = 24) -> datetime:
        return now + timedelta(hours=hours)
