"""Time.

AGENTS.md: "Timestamps stored UTC. Business logic timezone: Asia/Dubai
(decision D-001)."

edges.yaml#X-007: move_out_date is a *calendar day in Asia/Dubai*, stored as a
date, and every comparison against it uses the Dubai calendar. Nothing in this
module may call ``date.today()`` — that would use the host timezone.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

DUBAI = ZoneInfo("Asia/Dubai")  # decision D-001
UTC = timezone.utc


class Clock(Protocol):
    """Injectable clock so time-dependent rules (EXIT-05) are testable."""

    def now_utc(self) -> datetime: ...

    def today_dubai(self) -> date: ...


class SystemClock:
    """Real time."""

    def now_utc(self) -> datetime:
        return datetime.now(UTC)

    def today_dubai(self) -> date:
        # edges.yaml#X-007 — the tenant's calendar day, not the server's.
        return datetime.now(UTC).astimezone(DUBAI).date()


class FrozenClock:
    """Fixed time, for tests and for replaying a decision at a known instant."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FrozenClock requires an aware datetime")
        self._instant = instant.astimezone(UTC)

    def now_utc(self) -> datetime:
        return self._instant

    def today_dubai(self) -> date:
        return self._instant.astimezone(DUBAI).date()

    def advance(self, **timedelta_kwargs) -> None:
        from datetime import timedelta

        self._instant = self._instant + timedelta(**timedelta_kwargs)
