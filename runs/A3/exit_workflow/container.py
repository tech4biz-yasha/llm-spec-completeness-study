"""Composition root.

Adapters are chosen once at startup and hung off ``app.state``; nothing in the
service layer imports a concrete adapter. Tests swap any of these fields.
"""

from __future__ import annotations

from dataclasses import dataclass

from exit_workflow.core.config import Settings
from exit_workflow.integrations.agencies import (
    AgencyDirectory,
    HttpAgencyDirectory,
    StaticAgencyDirectory,
)
from exit_workflow.integrations.contracts import (
    ContractDirectory,
    HttpContractDirectory,
    StaticContractDirectory,
)
from exit_workflow.integrations.payments import (
    HttpPaymentGateway,
    PaymentGateway,
    SimulatedPaymentGateway,
)
from exit_workflow.services.events import EventPublisher, build_publisher
from exit_workflow.services.notifications import EmailSender, LoggingEmailSender
from exit_workflow.services.storage import DocumentStorage, LocalFileStorage


@dataclass
class AppContainer:
    settings: Settings
    storage: DocumentStorage
    contracts: ContractDirectory
    agencies: AgencyDirectory
    gateway: PaymentGateway
    publisher: EventPublisher
    email_sender: EmailSender


def build_container(settings: Settings) -> AppContainer:
    token = (
        settings.internal_service_token.get_secret_value()
        if settings.internal_service_token
        else None
    )
    contracts: ContractDirectory = (
        HttpContractDirectory(
            settings.property_service_url,
            token=token,
            timeout=settings.upstream_timeout_seconds,
        )
        if settings.property_service_url
        else StaticContractDirectory()
    )
    agencies: AgencyDirectory = (
        HttpAgencyDirectory(
            settings.agency_service_url,
            token=token,
            timeout=settings.upstream_timeout_seconds,
        )
        if settings.agency_service_url
        else StaticAgencyDirectory()
    )
    gateway: PaymentGateway = (
        HttpPaymentGateway(
            settings.payment_service_url,
            token=token,
            timeout=settings.payment_timeout_seconds,
        )
        if settings.payment_service_url
        else SimulatedPaymentGateway()
    )
    return AppContainer(
        settings=settings,
        storage=LocalFileStorage(settings.storage_root),
        contracts=contracts,
        agencies=agencies,
        gateway=gateway,
        publisher=build_publisher(settings),
        email_sender=LoggingEmailSender(),
    )
