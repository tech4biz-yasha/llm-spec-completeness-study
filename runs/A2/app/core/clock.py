"""Injectable clock.

Time-dependent policy (notice periods, dispute windows, auto-completion) is tested by
substituting a ``FrozenClock``; production code never calls ``datetime.now`` directly.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def today(self) -> date: ...


class SystemClock:
    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def today(self) -> date:
        return self.now().date()


class FrozenClock:
    """Deterministic clock for tests."""

    __slots__ = ("_now",)

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FrozenClock requires a timezone-aware datetime")
        self._now = now.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.date()

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    """Normalise a datetime to UTC, rejecting naive values."""
    if value.tzinfo is None:
        raise ValueError("naive datetimes are not accepted; supply a UTC offset")
    return value.astimezone(timezone.utc)
