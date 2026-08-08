"""Audit trail writer (SRS A3: complete audit trails, 7-year retention)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.core.clock import Clock
from app.core.config import Settings
from app.core.context import RequestContext
from app.models.audit import AuditLogEntry
from app.models.exit_workflow import ExitWorkflow
from app.repositories.support import AuditRepository

#: Never written to the audit trail, even if a caller passes them in a change set.
REDACTED_KEYS = frozenset(
    {
        "payout_account_ref",
        "iban",
        "account_number",
        "bank_account",
        "card_number",
        "password",
        "token",
        "authorization",
        "api_key",
        "secret",
    }
)
REDACTED = "[redacted]"
_MAX_STRING = 4000


def _scrub(value: Any, key: str | None = None) -> Any:
    """Make a value JSON-safe and strip anything sensitive."""
    if key is not None and key.lower() in REDACTED_KEYS:
        return REDACTED
    if isinstance(value, dict):
        return {str(k): _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_scrub(v) for v in value]
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "..."
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if hasattr(value, "value"):  # enums
        return _scrub(value.value)
    return str(value)


def add_years(value: date, years: int) -> date:
    """Add whole years, clamping 29 February onto 28 February in non-leap years."""
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, month=2, day=28)


class AuditService:
    def __init__(self, session: Any, settings: Settings, clock: Clock) -> None:
        self._repo = AuditRepository(session)
        self._settings = settings
        self._clock = clock

    def record(
        self,
        ctx: RequestContext,
        *,
        action: str,
        entity_type: str,
        entity_id: str | uuid.UUID | None = None,
        workflow: ExitWorkflow | None = None,
        workflow_id: uuid.UUID | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        changes: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditLogEntry:
        now = self._clock.now()
        entry = AuditLogEntry(
            occurred_at=now,
            workflow_id=workflow.id if workflow is not None else workflow_id,
            workflow_reference=workflow.reference if workflow is not None else None,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id is not None else None,
            actor_id=ctx.principal.actor_id,
            actor_role=ctx.principal.role,
            on_behalf_of=uuid.UUID(ctx.on_behalf_of) if ctx.on_behalf_of else None,
            from_state=from_state,
            to_state=to_state,
            changes=_scrub(changes or {}),
            context=_scrub(context or {}),
            request_id=ctx.request_id,
            ip_address=ctx.ip_address,
            user_agent=ctx.user_agent,
            retention_until=add_years(now.date(), self._settings.audit_retention_years),
        )
        self._repo.add(entry)
        return entry

    @staticmethod
    def diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        """Field-level change set, restricted to keys that actually moved."""
        changed: dict[str, Any] = {}
        for key in set(before) | set(after):
            old, new = before.get(key), after.get(key)
            if old != new:
                changed[key] = {"from": _scrub(old, key), "to": _scrub(new, key)}
        return changed
