"""Single source of time so that tests can freeze it."""

from __future__ import annotations

from datetime import UTC, date, datetime

_override: datetime | None = None


def utcnow() -> datetime:
    return _override or datetime.now(UTC)


def today() -> date:
    return utcnow().date()


def set_override(value: datetime | None) -> None:
    """Test hook. Never called from application code."""

    global _override
    _override = value
