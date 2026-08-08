"""Notification composition.

Services describe *who* should hear about *what*; delivery is the notifier's problem and
happens after commit (see :mod:`app.services.unit_of_work`).
"""

from __future__ import annotations

import uuid
from typing import Any

from app.models.exit_workflow import ExitWorkflow
from app.ports.notifications import (
    Notification,
    NotificationChannel,
    NotificationTemplate,
    Notifier,
    Recipient,
)
from app.services.unit_of_work import UnitOfWork

EMAIL_AND_PUSH = (NotificationChannel.EMAIL, NotificationChannel.PUSH)
EMAIL_ONLY = (NotificationChannel.EMAIL,)


def _recipient(snapshot: dict[str, Any], actor_id: uuid.UUID | None) -> Recipient:
    return Recipient(
        actor_id=str(actor_id) if actor_id else None,
        email=snapshot.get("email"),
        phone=snapshot.get("phone"),
        name=snapshot.get("name"),
    )


def tenant_recipient(workflow: ExitWorkflow) -> Recipient:
    return _recipient(workflow.tenant_snapshot or {}, workflow.tenant_id)


def owner_recipient(workflow: ExitWorkflow) -> Recipient:
    return _recipient(workflow.owner_snapshot or {}, workflow.owner_id)


def base_context(workflow: ExitWorkflow) -> dict[str, Any]:
    """Fields every template can rely on."""
    property_snapshot = workflow.property_snapshot or {}
    return {
        "workflow_id": str(workflow.id),
        "workflow_reference": workflow.reference,
        "state": workflow.state.value,
        "property_reference": property_snapshot.get("reference"),
        "property_address": property_snapshot.get("address"),
        "move_out_date": workflow.move_out_date.isoformat() if workflow.move_out_date else None,
        "tenant_name": (workflow.tenant_snapshot or {}).get("name"),
        "owner_name": (workflow.owner_snapshot or {}).get("name"),
    }


class NotificationService:
    def __init__(self, notifier: Notifier, uow: UnitOfWork) -> None:
        self._notifier = notifier
        self._uow = uow

    def enqueue(
        self,
        *,
        template: NotificationTemplate,
        recipients: tuple[Recipient, ...],
        context: dict[str, Any],
        channels: tuple[NotificationChannel, ...] = EMAIL_AND_PUSH,
        dedupe_key: str | None = None,
    ) -> None:
        """Queue a notification for delivery once the transaction commits."""
        addressable = tuple(r for r in recipients if r.email or r.phone or r.actor_id)
        if not addressable:
            return
        notification = Notification(
            template=template,
            channels=channels,
            recipients=addressable,
            context=context,
            dedupe_key=dedupe_key,
        )

        async def _send() -> None:
            await self._notifier.send(notification)

        self._uow.after_commit(f"notify:{template.value}", _send)
