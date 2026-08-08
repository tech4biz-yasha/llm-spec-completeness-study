"""FastAPI dependencies: settings, session, authentication and request context."""

from __future__ import annotations

from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends, Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.errors import AuthenticationError
from app.models.catalog import InspectionAgency
from app.security import Principal, PrincipalRole, decode_token, hash_api_key, principal_from_claims
from app.services.context import RequestContext

SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_principal(
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_agency_key: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the caller from either an agency API key or a bearer token.

    The agency key is checked first so an agency integration never needs a user token.
    """
    if x_agency_key:
        agency = await session.scalar(
            sa.select(InspectionAgency).where(
                InspectionAgency.api_key_hash == hash_api_key(x_agency_key)
            )
        )
        if agency is None:
            raise AuthenticationError("unknown agency API key")
        if not agency.is_active:
            raise AuthenticationError("agency API key is disabled")
        return Principal(id=agency.id, role=PrincipalRole.AGENCY, agency_id=agency.id)

    if not authorization:
        raise AuthenticationError("missing credentials")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthenticationError("expected an 'Authorization: Bearer <token>' header")

    claims = decode_token(
        token,
        settings.jwt_secret,
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        leeway_seconds=settings.jwt_leeway_seconds,
    )
    return principal_from_claims(claims)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def _client_ip(request: Request) -> str | None:
    # Trust the left-most X-Forwarded-For entry only behind a proxy that sets it.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


async def get_request_context(request: Request, principal: PrincipalDep) -> RequestContext:
    return RequestContext(
        principal=principal,
        request_id=getattr(request.state, "request_id", None),
        ip_address=_client_ip(request),
        user_agent=(request.headers.get("user-agent") or None),
    )


ContextDep = Annotated[RequestContext, Depends(get_request_context)]
