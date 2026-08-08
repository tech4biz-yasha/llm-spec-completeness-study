"""Time.

AGENTS.md: "Timestamps stored UTC. Business logic timezone: Asia/Dubai
(decision D-001)."

edges.yaml#X-007: ``move_out_date`` is a calendar day in Asia/Dubai, stored as a
``date``, and every comparison against it uses the Dubai calendar. The two are
not interchangeable: at 21:00 UTC it is already tomorrow in Dubai, so a date
that is "today" by UTC reckoning can be in the past for a tenant standing in
Dubai, and vice versa.

The :class:`Clock` indirection exists so that date-boundary behaviour is
testable without freezing the process clock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Final, Protocol
from zoneinfo import ZoneInfo

#: decision D-001.
DUBAI: Final[ZoneInfo] = ZoneInfo("Asia/Dubai")
UTC: Final[timezone] = timezone.utc


class Clock(Protocol):
    """Source of the current instant."""

    def now_utc(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""


class SystemClock:
    """The real clock."""

    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC)


class FixedClock:
    """A clock pinned to one instant, for tests and for deterministic replays."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._instant


DEFAULT_CLOCK: Final[SystemClock] = SystemClock()


def today_dubai(clock: Clock = DEFAULT_CLOCK) -> date:
    """Return the current calendar day in Asia/Dubai (edges.yaml#X-007)."""
    return clock.now_utc().astimezone(DUBAI).date()


def now_utc(clock: Clock = DEFAULT_CLOCK) -> datetime:
    """Return the current instant in UTC, for storage."""
    return clock.now_utc().astimezone(UTC)


def days_between(earlier: date, later: date) -> int:
    """Whole Dubai calendar days from ``earlier`` to ``later``."""
    return (later - earlier).days


def add_days(day: date, days: int) -> date:
    """Return the Dubai calendar day ``days`` after ``day``."""
    return day + timedelta(days=days)
