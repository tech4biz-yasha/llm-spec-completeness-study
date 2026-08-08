"""Authentication primitives: bearer JWT -> :class:`Principal`.

Tokens are issued by the platform identity service; this module only verifies
them. ``issue_token`` exists for local development and tests and is never
reachable over HTTP.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any

import jwt

from exit_workflow.core.clock import utcnow
from exit_workflow.core.config import Settings, get_settings
from exit_workflow.core.errors import ForbiddenError, UnauthorizedError


class Role(StrEnum):
    TENANT = "TENANT"
    OWNER = "OWNER"
    INSPECTION_AGENCY = "INSPECTION_AGENCY"
    ADMIN = "ADMIN"
    SERVICE = "SERVICE"


@dataclass(frozen=True, slots=True)
class Principal:
    subject_id: uuid.UUID
    role: Role
    email: str | None = None
    #: For INSPECTION_AGENCY staff: the agency they act for. For other roles it
    #: is unset. An agency user may only touch inspections routed to this id.
    org_id: uuid.UUID | None = None
    scopes: frozenset[str] = frozenset()
    token_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role in (Role.ADMIN, Role.SERVICE)

    @property
    def audit_actor(self) -> str:
        return f"{self.role}:{self.subject_id}"

    def require_role(self, *roles: Role) -> None:
        if self.role in roles or self.is_admin:
            return
        raise ForbiddenError(
            f"This action requires one of: {', '.join(sorted(r.value for r in roles))}."
        )

    def agency_scope(self) -> uuid.UUID:
        """The agency this principal acts for (agency users only)."""

        if self.role is not Role.INSPECTION_AGENCY:
            raise ForbiddenError("Only inspection agency users may act on inspections.")
        if self.org_id is None:
            raise ForbiddenError("Agency token is missing the org_id claim.")
        return self.org_id


def _parse_uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise UnauthorizedError(f"Token claim {field!r} is not a valid UUID.") from exc


def decode_token(token: str, settings: Settings | None = None) -> Principal:
    settings = settings or get_settings()
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("Access token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthorizedError("Access token is invalid.") from exc

    raw_role = claims.get("role")
    try:
        role = Role(str(raw_role).upper())
    except ValueError as exc:
        raise UnauthorizedError(f"Unknown role {raw_role!r} in token.") from exc

    org_id = claims.get("org_id")
    scopes = claims.get("scope") or ""
    if isinstance(scopes, str):
        scopes = scopes.split()

    return Principal(
        subject_id=_parse_uuid(claims["sub"], "sub"),
        role=role,
        email=claims.get("email"),
        org_id=_parse_uuid(org_id, "org_id") if org_id else None,
        scopes=frozenset(scopes),
        token_id=claims.get("jti"),
    )


def issue_token(
    *,
    subject_id: uuid.UUID,
    role: Role,
    email: str | None = None,
    org_id: uuid.UUID | None = None,
    scopes: list[str] | None = None,
    expires_in: timedelta = timedelta(hours=1),
    settings: Settings | None = None,
) -> str:
    """Dev/test helper mirroring what the identity service issues."""

    settings = settings or get_settings()
    now = utcnow()
    claims: dict[str, Any] = {
        "sub": str(subject_id),
        "role": role.value,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    if email:
        claims["email"] = email
    if org_id:
        claims["org_id"] = str(org_id)
    if scopes:
        claims["scope"] = " ".join(scopes)
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)
