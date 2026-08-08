"""Authentication and request-signature verification."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt

from app.core.config import Settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.domain.enums import ActorRole


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    actor_id: uuid.UUID
    role: ActorRole
    #: Agency principals carry the id of the inspection agency they act for.
    agency_id: uuid.UUID | None = None
    scopes: frozenset[str] = frozenset()
    token_id: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role is ActorRole.ADMIN

    def require_scope(self, scope: str) -> None:
        if self.is_admin or not self.scopes or scope in self.scopes:
            return
        raise AuthorizationError(
            f"Token is missing the required scope '{scope}'.",
            details={"required_scope": scope},
        )


SYSTEM_PRINCIPAL = Principal(
    actor_id=uuid.UUID("00000000-0000-0000-0000-000000000000"),
    role=ActorRole.SYSTEM,
)


def decode_token(token: str, settings: Settings) -> Principal:
    """Verify a bearer token and map its claims onto a :class:`Principal`."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_verification_key,
            algorithms=[settings.jwt_algorithm],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("The access token has expired.", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("The access token is invalid.") from exc

    try:
        actor_id = uuid.UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise AuthenticationError("Token subject is not a valid actor id.") from exc

    raw_role = str(claims.get("role", "")).upper()
    try:
        role = ActorRole(raw_role)
    except ValueError as exc:
        raise AuthenticationError(
            f"Token carries an unknown role {raw_role!r}.",
        ) from exc

    if role is ActorRole.SYSTEM:
        # SYSTEM is an internal-only role used by the reconciler and webhook handlers;
        # it must never be reachable with a user-issued token.
        raise AuthenticationError("The SYSTEM role cannot be assumed by a bearer token.")

    agency_id: uuid.UUID | None = None
    if raw_agency := claims.get("agency_id"):
        try:
            agency_id = uuid.UUID(str(raw_agency))
        except ValueError as exc:
            raise AuthenticationError("Token carries an invalid agency_id.") from exc
    if role is ActorRole.INSPECTION_AGENCY and agency_id is None:
        raise AuthenticationError("Inspection agency tokens must carry an agency_id claim.")

    scopes_claim = claims.get("scope") or claims.get("scopes") or ""
    scopes = (
        frozenset(scopes_claim.split())
        if isinstance(scopes_claim, str)
        else frozenset(str(s) for s in scopes_claim)
    )

    return Principal(
        actor_id=actor_id,
        role=role,
        agency_id=agency_id,
        scopes=scopes,
        token_id=claims.get("jti"),
    )


# --------------------------------------------------------------- webhooks
def compute_webhook_signature(secret: str, timestamp: str, body: bytes) -> str:
    """``HMAC-SHA256(secret, "<timestamp>.<body>")``, hex encoded."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return mac.hexdigest()


def verify_webhook_signature(
    *,
    secret: str,
    signature_header: str | None,
    timestamp_header: str | None,
    body: bytes,
    tolerance_seconds: int,
    now: float | None = None,
) -> None:
    """Verify a provider webhook.

    Signing over a timestamp as well as the body means a captured callback cannot be
    replayed later to re-confirm a payout.
    """
    if not signature_header or not timestamp_header:
        raise AuthenticationError(
            "Webhook signature headers are missing.", code="webhook_signature_missing"
        )
    try:
        sent_at = int(timestamp_header)
    except ValueError as exc:
        raise AuthenticationError("Webhook timestamp is malformed.") from exc

    current = now if now is not None else time.time()
    if abs(current - sent_at) > tolerance_seconds:
        raise AuthenticationError(
            "Webhook timestamp is outside the accepted window.",
            code="webhook_timestamp_expired",
        )

    expected = compute_webhook_signature(secret, timestamp_header, body)
    provided = signature_header.strip()
    # Providers commonly prefix the scheme, e.g. "sha256=<hex>".
    if "=" in provided and provided.split("=", 1)[0].lower().startswith("sha"):
        provided = provided.split("=", 1)[1]
    if not hmac.compare_digest(expected, provided):
        raise AuthenticationError(
            "Webhook signature verification failed.", code="webhook_signature_invalid"
        )


# ------------------------------------------------------------ misc tokens
_VERIFICATION_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1


def generate_verification_code(length: int = 12, group: int = 4) -> str:
    """A human-transcribable NOC verification code, e.g. ``K7QW-3M2X-9RTP``."""
    raw = "".join(secrets.choice(_VERIFICATION_ALPHABET) for _ in range(length))
    return "-".join(raw[i : i + group] for i in range(0, length, group))


def hash_request_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()
