"""Kafka publication.

AGENTS.md names a Kafka producer for events. The :class:`EventPublisher`
protocol keeps the retry and dead-letter policy of rules.yaml#EXIT-04 in
:mod:`exit_workflow.events.dispatcher`, independent of the transport.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """The event could not be handed to the broker."""


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Money crosses the wire as a string so no consumer can parse it as a
        # float (AGENTS.md: never float).
        return str(value)
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, default=_json_default, separators=(",", ":")).encode("utf-8")


class EventPublisher(Protocol):
    """Transport for workflow events."""

    async def publish(self, *, topic: str, key: str, payload: dict[str, Any]) -> None:
        """Publish one event, raising :class:`PublishError` on failure."""


class KafkaEventPublisher:
    """aiokafka-backed publisher."""

    def __init__(self, bootstrap_servers: str, *, client_id: str = "exit-workflow") -> None:
        self._bootstrap_servers = bootstrap_servers
        self._client_id = client_id
        self._producer: Any | None = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        if self._producer is None:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                client_id=self._client_id,
                enable_idempotence=True,
                acks="all",
            )
            await self._producer.start()

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish(self, *, topic: str, key: str, payload: dict[str, Any]) -> None:
        if self._producer is None:
            raise PublishError("Kafka producer is not started")
        try:
            await self._producer.send_and_wait(topic, key=key.encode("utf-8"), value=encode(payload))
        except Exception as exc:  # aiokafka raises a wide family of errors
            raise PublishError(f"failed to publish to {topic}: {exc}") from exc


class InMemoryEventPublisher:
    """Records events instead of sending them.

    For local runs with ``EXIT_KAFKA_ENABLED=false`` and for tests. Not a
    fallback in production: :func:`exit_workflow.app.build_publisher` refuses to
    substitute it there, because silently dropping owner notifications would
    look exactly like success.
    """

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, *, topic: str, key: str, payload: dict[str, Any]) -> None:
        encode(payload)  # same serialisation failures as the real transport
        self.published.append({"topic": topic, "key": key, "payload": payload})
        logger.debug("in-memory publish to %s key=%s", topic, key)
