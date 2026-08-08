"""A small in-process rate limiter for the unauthenticated endpoints.

This is a backstop, not the primary control: with more than one replica the real limit
belongs at the API gateway. It exists so a single instance cannot be used to brute-force
NOC verification codes even if the gateway rule is missing.
"""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from fastapi import Request

from app.core.config import get_settings
from app.core.errors import RateLimitedError

_WINDOW_SECONDS = 60
#: Bound on tracked clients; the oldest bucket is dropped when the cap is reached, which
#: keeps the limiter from becoming a memory-exhaustion vector itself.
_MAX_TRACKED_CLIENTS = 10_000


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int = _WINDOW_SECONDS) -> None:
        self._limit = limit
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str, *, now: float | None = None) -> None:
        current = now if now is not None else time.monotonic()
        cutoff = current - self._window
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= _MAX_TRACKED_CLIENTS:
                    self._hits.pop(next(iter(self._hits)), None)
                bucket = deque()
                self._hits[key] = bucket
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._limit:
                retry_after = max(1, int(self._window - (current - bucket[0])))
                raise RateLimitedError(retry_after_seconds=retry_after)
            bucket.append(current)


_limiter: SlidingWindowLimiter | None = None


def _get_limiter() -> SlidingWindowLimiter:
    global _limiter
    if _limiter is None:
        _limiter = SlidingWindowLimiter(
            get_settings().noc_verification_rate_limit_per_minute
        )
    return _limiter


def public_rate_limit(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    _get_limiter().check(client)


def reset_rate_limiter() -> None:
    """Test hook."""
    global _limiter
    _limiter = None
