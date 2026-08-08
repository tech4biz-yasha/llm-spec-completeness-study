"""FastAPI dependencies.

Collaborators live on ``app.state`` so that a deployment (or a test) can bind a
real Kafka producer, gateway and bucket, or fakes, without this module knowing
which it got.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from exit_workflow.config import Settings
from exit_workflow.domain.clock import Clock
from exit_workflow.domain.principal import Principal
from exit_workflow.domain.reasons import ExitReasonReference
from exit_workflow.events.dispatcher import OutboxDispatcher
from exit_workflow.gateway.payments import PaymentGateway
from exit_workflow.storage.noc import NocStorage


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request.

    Handlers commit explicitly: initiation has to know the exact moment its
    transaction lands, because rules.yaml#EXIT-04 puts the owner notification
    strictly after it.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


def get_reasons(request: Request) -> ExitReasonReference:
    return request.app.state.exit_reasons


def get_gateway(request: Request) -> PaymentGateway:
    return request.app.state.payment_gateway


def get_noc_storage(request: Request) -> NocStorage:
    return request.app.state.noc_storage


def get_dispatcher(request: Request) -> OutboxDispatcher:
    return request.app.state.dispatcher


async def get_principal(request: Request) -> Principal:
    """Resolve the authenticated caller (see :mod:`exit_workflow.api.security`)."""
    return await request.app.state.principal_resolver.resolve(request)


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
ClockDep = Annotated[Clock, Depends(get_clock)]
ReasonsDep = Annotated[ExitReasonReference, Depends(get_reasons)]
GatewayDep = Annotated[PaymentGateway, Depends(get_gateway)]
NocStorageDep = Annotated[NocStorage, Depends(get_noc_storage)]
DispatcherDep = Annotated[OutboxDispatcher, Depends(get_dispatcher)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
