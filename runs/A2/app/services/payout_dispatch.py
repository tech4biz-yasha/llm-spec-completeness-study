"""Post-commit payout dispatch.

Runs outside the request transaction (see :mod:`app.services.settlement` for why) and
owns its own session. Safe to call more than once for the same settlement: the provider
is given the settlement's stable idempotency key, and a settlement that is no longer
PROCESSING is skipped.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import system_context
from app.core.logging import get_logger
from app.domain.enums import SettlementStatus
from app.models.settlement import Settlement
from app.ports.payments import PaymentGateway, PaymentGatewayError, PayoutRequest, PayoutState

log = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class PayoutDispatchService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        settings: Settings,
        clock: Clock,
        payments: PaymentGateway,
        service_builder: Callable[..., object] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._clock = clock
        self._payments = payments
        self._service_builder = service_builder

    async def dispatch(self, workflow_id: uuid.UUID, request_id: str | None = None) -> None:
        from app.services.factory import build_services  # noqa: PLC0415 - breaks a cycle

        ctx = system_context(request_id)
        async with self._session_factory() as session:
            services = build_services(session, settings=self._settings, clock=self._clock)
            workflow = await services.workflows_repo.get_for_update(workflow_id)
            settlement = await services.settlements_repo.get_for_workflow_for_update(
                workflow_id
            )
            if settlement is None or settlement.status is not SettlementStatus.PROCESSING:
                log.info(
                    "payout.dispatch.skipped",
                    workflow_id=str(workflow_id),
                    status=settlement.status.value if settlement else None,
                )
                return

            request = self._build_request(settlement, workflow_reference=workflow.reference)
            try:
                result = await self._payments.initiate_payout(request)
            except PaymentGatewayError as exc:
                log.warning(
                    "payout.dispatch.failed",
                    workflow_id=str(workflow_id),
                    retryable=exc.retryable,
                    error=str(exc),
                )
                if exc.retryable:
                    # Leave the settlement in PROCESSING; the reconciler retries with
                    # the same idempotency key.
                    return
                await services.settlements.fail_settlement(
                    workflow,
                    settlement,
                    ctx=ctx,
                    failure_code="gateway_rejected",
                    failure_reason=str(exc)[:1000],
                )
                await services.uow.commit()
                return

            settlement.payment_provider = result.provider
            settlement.payment_reference = result.provider_reference

            if result.state is PayoutState.SUCCEEDED:
                await services.settlements.confirm_settlement(
                    workflow, settlement, ctx=ctx, provider_reference=result.provider_reference
                )
            elif result.state is PayoutState.FAILED:
                await services.settlements.fail_settlement(
                    workflow,
                    settlement,
                    ctx=ctx,
                    failure_code=result.failure_code,
                    failure_reason=result.failure_reason,
                )
            else:
                log.info(
                    "payout.dispatch.pending",
                    workflow_id=str(workflow_id),
                    provider_reference=result.provider_reference,
                )
            await services.uow.commit()

    def _build_request(
        self, settlement: Settlement, *, workflow_reference: str | None
    ) -> PayoutRequest:
        assert settlement.payment_idempotency_key is not None
        assert settlement.payout_account_ref is not None
        return PayoutRequest(
            idempotency_key=settlement.payment_idempotency_key,
            amount=settlement.net_refund_amount,
            currency=settlement.currency,
            beneficiary_ref=settlement.payout_account_ref,
            reference=workflow_reference or str(settlement.workflow_id),
            description=(
                f"Security deposit refund for exit workflow "
                f"{workflow_reference or settlement.workflow_id}"
            ),
            metadata={
                "workflow_id": str(settlement.workflow_id),
                "settlement_id": str(settlement.id),
            },
        )
