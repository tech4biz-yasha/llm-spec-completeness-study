"""Event publishers. rules.yaml#EXIT-04.

``KafkaEventPublisher`` is the production adapter (AGENTS.md, Stack: "Kafka producer for
events"). ``InMemoryEventPublisher`` and ``FailingEventPublisher`` exist for tests and for
the edges.yaml#X-002 dispatch-failure path.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..ports.events import Event, PublishError

logger = logging.getLogger(__name__)


class InMemoryEventPublisher:
    """Records published events. Used by tests and by local development."""

    def __init__(self) -> None:
        self.published: list[Event] = []

    def publish(self, event: Event) -> None:
        self.published.append(event)


class FailingEventPublisher:
    """Always raises. edges.yaml#X-002 — the workflow must survive this."""

    def __init__(self, message: str = "kafka unreachable") -> None:
        self.message = message
        self.attempts = 0

    def publish(self, event: Event) -> None:
        self.attempts += 1
        raise PublishError(self.message)


class LoggingEventPublisher:
    """Writes the event to the application log. For environments without a broker."""

    def publish(self, event: Event) -> None:
        logger.info("event topic=%s key=%s payload=%s", event.topic, event.key, event.payload)


class KafkaEventPublisher:
    """confluent-kafka producer, flushed synchronously so the outbox learns the outcome.

    ``confluent-kafka`` is an optional extra; the import is deferred so the module runs
    without a broker installed.
    """

    def __init__(self, producer_config: dict[str, Any], *, flush_timeout: float = 10.0) -> None:
        try:
            from confluent_kafka import Producer
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "KafkaEventPublisher requires the 'kafka' extra: pip install exit-workflow[kafka]"
            ) from exc
        self._producer = Producer(producer_config)
        self._flush_timeout = flush_timeout

    def publish(self, event: Event) -> None:  # pragma: no cover - requires a broker
        failures: list[str] = []

        def _on_delivery(err: Any, _msg: Any) -> None:
            if err is not None:
                failures.append(str(err))

        try:
            self._producer.produce(
                topic=event.topic,
                key=event.key.encode("utf-8"),
                value=json.dumps(event.payload, separators=(",", ":")).encode("utf-8"),
                on_delivery=_on_delivery,
            )
            remaining = self._producer.flush(self._flush_timeout)
        except Exception as exc:
            raise PublishError(f"kafka produce failed: {exc}") from exc
        if remaining:
            raise PublishError(f"kafka flush timed out with {remaining} message(s) in flight")
        if failures:
            raise PublishError(f"kafka delivery failed: {'; '.join(failures)}")
