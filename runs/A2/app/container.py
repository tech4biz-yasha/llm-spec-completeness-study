"""Process-wide port singletons.

Adapters are chosen once from configuration and shared: they hold connection pools
(HTTP clients, Kafka producers) that must not be rebuilt per request. Tests call
:func:`configure_ports` to substitute fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.adapters.event_publisher import KafkaEventPublisher, LoggingEventPublisher
from app.adapters.noc_renderer_pdf import PdfNocRenderer
from app.adapters.notifier_log import LoggingNotifier, WebhookNotifier
from app.adapters.payments import HttpPaymentGateway, NullPaymentGateway
from app.adapters.storage_local import LocalDocumentStorage
from app.core.clock import Clock, SystemClock
from app.core.config import Settings, get_settings
from app.ports.event_publisher import EventPublisher
from app.ports.noc_renderer import NocRenderer
from app.ports.notifications import Notifier
from app.ports.payments import PaymentGateway
from app.ports.storage import DocumentStorage


@dataclass(frozen=True, slots=True)
class Ports:
    clock: Clock
    storage: DocumentStorage
    notifier: Notifier
    payments: PaymentGateway
    events: EventPublisher
    noc_renderer: NocRenderer


_ports: Ports | None = None


def build_ports(settings: Settings | None = None) -> Ports:
    settings = settings or get_settings()

    storage: DocumentStorage
    if settings.storage_backend == "s3":
        from app.adapters.storage_s3 import S3DocumentStorage  # noqa: PLC0415

        if not settings.storage_s3_bucket:
            raise RuntimeError("storage_backend=s3 requires EXITFLOW_STORAGE_S3_BUCKET")
        storage = S3DocumentStorage(
            bucket=settings.storage_s3_bucket,
            region=settings.storage_s3_region,
            endpoint_url=settings.storage_s3_endpoint_url,
        )
    else:
        storage = LocalDocumentStorage(settings.storage_local_root)

    notifier: Notifier
    if settings.notification_backend == "http" and settings.notification_webhook_url:
        notifier = WebhookNotifier(settings.notification_webhook_url)
    else:
        notifier = LoggingNotifier()

    payments: PaymentGateway
    if settings.payment_backend == "http":
        if not (settings.payment_api_base_url and settings.payment_api_key):
            raise RuntimeError(
                "payment_backend=http requires EXITFLOW_PAYMENT_API_BASE_URL and "
                "EXITFLOW_PAYMENT_API_KEY"
            )
        payments = HttpPaymentGateway(
            base_url=settings.payment_api_base_url,
            api_key=settings.payment_api_key,
            timeout=settings.payment_request_timeout_seconds,
        )
    else:
        payments = NullPaymentGateway()

    events: EventPublisher
    if settings.event_publisher == "kafka":
        if not settings.kafka_bootstrap_servers:
            raise RuntimeError("event_publisher=kafka requires EXITFLOW_KAFKA_BOOTSTRAP_SERVERS")
        events = KafkaEventPublisher(
            settings.kafka_bootstrap_servers, client_id=settings.app_name
        )
    else:
        events = LoggingEventPublisher()

    return Ports(
        clock=SystemClock(),
        storage=storage,
        notifier=notifier,
        payments=payments,
        events=events,
        noc_renderer=PdfNocRenderer(),
    )


def get_ports() -> Ports:
    global _ports
    if _ports is None:
        _ports = build_ports()
    return _ports


def configure_ports(ports: Ports | None = None, **overrides: object) -> Ports:
    """Install ports explicitly. Used by the app lifespan and by tests."""
    global _ports
    base = ports or _ports or build_ports()
    _ports = replace(base, **overrides) if overrides else base  # type: ignore[arg-type]
    return _ports


def reset_ports() -> None:
    global _ports
    _ports = None
