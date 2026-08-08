"""Time.

AGENTS.md, Conventions: "Timestamps stored UTC. Business logic timezone: Asia/Dubai
(decision D-001)." edges.yaml#X-007: move_out_date is a calendar day in Asia/Dubai,
stored as a date, and every comparison uses the Dubai calendar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Asia/Dubai")  # decision D-001
# ``UTC`` is re-exported from here so that every module reaches for the same one:
# AGENTS.md fixes storage at UTC and business logic at Asia/Dubai, and the two must never
# be confused at an import site.
__all__ = [
    "BUSINESS_TZ",
    "UTC",
    "Clock",
    "FrozenClock",
    "SystemClock",
    "business_date",
    "business_today",
    "days_between",
]


class Clock(Protocol):
    """Injected so that stall sweeps and date validation are testable."""

    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC)


class FrozenClock:
    """Test double. Not used in production paths."""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._moment = moment.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._moment

    def advance(self, delta: timedelta) -> None:
        self._moment += delta


def business_date(moment: datetime) -> date:
    """The Asia/Dubai calendar day containing ``moment``. edges.yaml#X-007."""
    if moment.tzinfo is None:
        raise ValueError("naive datetime; timestamps are stored and passed as UTC-aware")
    return moment.astimezone(BUSINESS_TZ).date()


def business_today(clock: Clock) -> date:
    """Today in Asia/Dubai. edges.yaml#X-007, decision D-001."""
    return business_date(clock.now_utc())


def days_between(earlier: date, later: date) -> int:
    """Whole Dubai calendar days from ``earlier`` to ``later``."""
    return (later - earlier).days
