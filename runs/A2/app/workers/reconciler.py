"""Periodic reconciliation.

Every mechanism in this module that can leave a workflow parked -- an abandoned draft, a
payout whose dispatch never ran, a NOC nobody collected -- is swept here. Without it,
those workflows keep the BR-1 lock on their property forever.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.container import Ports, get_ports
from app.core.clock import Clock
from app.core.config import Settings, get_settings
from app.core.context import system_context
from app.core.logging import get_logger
from app.db.session import session_scope
from app.domain.enums import ExitWorkflowState, SettlementStatus
from app.repositories.support import IdempotencyRepository
from app.schemas.exit_workflow import CompleteRequest
from app.services.factory import build_services
from app.services.payout_dispatch import PayoutDispatchService

log = get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
#: A payout stuck in PROCESSING for longer than this is re-driven at the provider using
#: the settlement's stable idempotency key, so it cannot pay twice.
STUCK_PAYOUT_MINUTES = 15


class Reconciler:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        clock: Clock | None = None,
        ports: Ports | None = None,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings or get_settings()
        self._ports = ports or get_ports()
        self._clock = clock or self._ports.clock
        self._interval = interval_seconds
        self._stopping = asyncio.Event()

    async def run_forever(self) -> None:
        log.info("reconciler.started", interval_seconds=self._interval)
        try:
            while not self._stopping.is_set():
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001 - the loop must survive anything
                    log.exception("reconciler.cycle_failed")
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
                except TimeoutError:
                    pass
        finally:
            log.info("reconciler.stopped")

    def stop(self) -> None:
        self._stopping.set()

    async def run_once(self) -> dict[str, int]:
        return {
            "expired_drafts": await self.expire_stale_drafts(),
            "auto_completed": await self.auto_complete_after_noc(),
            "payouts_redriven": await self.redrive_stuck_payouts(),
            "idempotency_purged": await self.purge_idempotency_keys(),
        }

    async def expire_stale_drafts(self) -> int:
        cutoff = self._clock.now() - timedelta(days=self._settings.draft_expiry_days)
        ctx = system_context()
        count = 0
        async with session_scope() as session:
            services = build_services(
                session, settings=self._settings, clock=self._clock, ports=self._ports
            )
            stale = await services.workflows_repo.find_stale(
                state=ExitWorkflowState.DRAFT, older_than=cutoff
            )
            for workflow in stale:
                await services.workflows.expire_draft(workflow, ctx)
                count += 1
            if count:
                await services.uow.commit()
        if count:
            log.info("reconciler.drafts_expired", count=count)
        return count

    async def auto_complete_after_noc(self) -> int:
        """Close workflows whose NOC has been available but uncollected.

        The SRS makes completion an explicit step; leaving it purely manual would let an
        inattentive party keep a property un-lettable indefinitely, so it is closed
        automatically after a grace period and the closure is attributed to SYSTEM.
        """
        cutoff = self._clock.now() - timedelta(
            days=self._settings.auto_complete_after_noc_days
        )
        ctx = system_context()
        count = 0
        async with session_scope() as session:
            services = build_services(
                session, settings=self._settings, clock=self._clock, ports=self._ports
            )
            candidates = await services.workflows_repo.find_stale(
                state=ExitWorkflowState.NOC_ISSUED, older_than=cutoff
            )
            for workflow in candidates:
                await services.workflows.complete(
                    workflow.id,
                    CompleteRequest(
                        note=(
                            "Automatically completed "
                            f"{self._settings.auto_complete_after_noc_days} days after "
                            "NOC issuance."
                        )
                    ),
                    ctx,
                )
                count += 1
            if count:
                await services.uow.commit()
        if count:
            log.info("reconciler.auto_completed", count=count)
        return count

    async def redrive_stuck_payouts(self) -> int:
        from sqlalchemy import select  # noqa: PLC0415

        from app.models.settlement import Settlement  # noqa: PLC0415

        cutoff = self._clock.now() - timedelta(minutes=STUCK_PAYOUT_MINUTES)
        dispatcher = PayoutDispatchService(
            session_factory=session_scope,
            settings=self._settings,
            clock=self._clock,
            payments=self._ports.payments,
        )

        async with session_scope() as session:
            stmt = (
                select(Settlement.workflow_id)
                .where(
                    Settlement.status == SettlementStatus.PROCESSING.value,
                    Settlement.payment_initiated_at < cutoff,
                )
                .limit(50)
            )
            workflow_ids = list((await session.execute(stmt)).scalars().all())

        for workflow_id in workflow_ids:
            try:
                await dispatcher.dispatch(workflow_id)
            except Exception:  # noqa: BLE001 - one bad payout must not stop the sweep
                log.exception("reconciler.payout_redrive_failed", workflow_id=str(workflow_id))
        if workflow_ids:
            log.info("reconciler.payouts_redriven", count=len(workflow_ids))
        return len(workflow_ids)

    async def purge_idempotency_keys(self) -> int:
        async with session_scope() as session:
            return await IdempotencyRepository(session).purge_expired(now=self._clock.now())
