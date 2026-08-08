"""Event bus port. rules.yaml#EXIT-04, edges.yaml#X-002."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class PublishError(RuntimeError):
    """Dispatch failed. Never rolls the workflow back (rules.yaml#EXIT-04)."""


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    key: str
    payload: dict[str, Any]


@runtime_checkable
class EventPublisher(Protocol):
    def publish(self, event: Event) -> None:
        """Publish, or raise PublishError. Must be idempotent on the consumer side:
        the outbox retries the same event key up to the configured attempt count."""
