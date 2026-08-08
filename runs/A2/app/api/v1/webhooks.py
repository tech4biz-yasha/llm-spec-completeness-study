"""Inbound webhooks."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import ValidationError

from app.api.deps import ServicesDep, SettingsDep
from app.core.context import system_context
from app.core.errors import ValidationFailedError
from app.core.logging import get_logger
from app.core.security import verify_webhook_signature
from app.domain.enums import SettlementStatus
from app.schemas.settlement import PaymentWebhookEvent

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

log = get_logger(__name__)

_SUCCESS = {"SUCCEEDED", "PAID", "COMPLETED", "SETTLED"}
_FAILURE = {"FAILED", "REJECTED", "CANCELLED", "RETURNED", "REVERSED"}


@router.post(
    "/payments",
    status_code=status.HTTP_200_OK,
    summary="Payment provider callback",
    description=(
        "Confirms or fails a deposit refund. Authenticated by an HMAC-SHA256 signature "
        "over `<timestamp>.<body>` in `X-Signature`, not by a bearer token. Delivery is "
        "assumed at-least-once: replays of an already-confirmed payout are accepted and "
        "ignored. On success the Exit NOC is generated in the same transaction."
    ),
)
async def payment_webhook(
    request: Request,
    services: ServicesDep,
    settings: SettingsDep,
    response: Response,
    signature: Annotated[str | None, Header(alias="X-Signature")] = None,
    timestamp: Annotated[str | None, Header(alias="X-Signature-Timestamp")] = None,
) -> dict[str, str]:
    body = await request.body()
    verify_webhook_signature(
        secret=settings.payment_webhook_secret,
        signature_header=signature,
        timestamp_header=timestamp,
        body=body,
        tolerance_seconds=settings.payment_webhook_tolerance_seconds,
    )

    try:
        event = PaymentWebhookEvent.model_validate_json(body)
    except ValidationError as exc:
        raise ValidationFailedError(
            "The webhook payload could not be parsed.",
            details={"errors": exc.errors(include_url=False)[:5]},
        ) from exc

    ctx = system_context(getattr(request.state, "request_id", None))
    settlement = await services.settlements_repo.find_by_payment_reference(
        event.payout_reference
    )
    if settlement is None:
        # Unknown reference: acknowledge so the provider stops retrying, and log it for
        # investigation. Returning 4xx here would have the provider hammer us forever.
        log.warning(
            "payment_webhook.unknown_reference",
            payout_reference=event.payout_reference,
            event_id=event.event_id,
        )
        response.status_code = status.HTTP_202_ACCEPTED
        return {"status": "ignored", "reason": "unknown_payout_reference"}

    workflow = await services.workflows_repo.get_for_update(settlement.workflow_id)
    outcome = event.status.strip().upper()

    if outcome in _SUCCESS:
        if settlement.status is SettlementStatus.COMPLETED:
            return {"status": "duplicate"}
        await services.settlements.confirm_settlement(
            workflow, settlement, ctx=ctx, provider_reference=event.payout_reference
        )
        return {"status": "confirmed"}

    if outcome in _FAILURE:
        await services.settlements.fail_settlement(
            workflow,
            settlement,
            ctx=ctx,
            failure_code=event.failure_code,
            failure_reason=event.failure_reason,
        )
        return {"status": "failed"}

    log.info(
        "payment_webhook.ignored_status",
        payout_reference=event.payout_reference,
        provider_status=outcome,
    )
    return {"status": "ignored"}
