"""Authentication primitives.

Access tokens are HS256 JWTs minted by the platform identity service; this module only
*verifies* them. Verification is implemented against ``hmac``/``hashlib`` directly rather than
pulling in a JWT library, so the module has no cryptographic dependency to keep patched, and
the accepted algorithm set is a hard-coded allowlist (HS256 only) — the "alg": "none" and
algorithm-confusion families of attack are structurally impossible here.

Inspection agencies authenticate with an API key instead, presented as ``X-Agency-Key``. Only
the SHA-256 of a key is ever stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.errors import AuthenticationError

ALGORITHM = "HS256"
_AGENCY_KEY_BYTES = 32


class PrincipalRole(StrEnum):
    TENANT = "TENANT"
    OWNER = "OWNER"
    AGENCY = "AGENCY"
    ADMIN = "ADMIN"


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    id: uuid.UUID
    role: PrincipalRole
    #: Present only for agency principals.
    agency_id: uuid.UUID | None = None
    claims: dict[str, Any] | None = None

    @property
    def is_admin(self) -> bool:
        return self.role is PrincipalRole.ADMIN


# --- base64url ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64url_decode(segment: bytes) -> bytes:
    padding = b"=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except Exception as exc:
        raise AuthenticationError("malformed token encoding") from exc


# --- JWT ---------------------------------------------------------------------------------


def encode_token(
    claims: dict[str, Any],
    secret: str,
    *,
    expires_in: timedelta | None = None,
) -> str:
    """Mint an HS256 token.

    Production traffic is verified, not minted, here; this exists for local development, the
    test-suite, and service-to-service tokens issued by trusted internal callers.
    """
    payload = dict(claims)
    now = datetime.now(UTC)
    payload.setdefault("iat", int(now.timestamp()))
    if expires_in is not None:
        payload["exp"] = int((now + expires_in).timestamp())

    header = _b64url_encode(json.dumps({"alg": ALGORITHM, "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), default=str).encode())
    signing_input = header + b"." + body
    signature = _b64url_encode(_sign(signing_input, secret))
    return (signing_input + b"." + signature).decode()


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()


def decode_token(
    token: str,
    secret: str,
    *,
    issuer: str | None = None,
    audience: str | None = None,
    leeway_seconds: int = 0,
) -> dict[str, Any]:
    """Verify signature and standard claims, returning the payload.

    Raises:
        AuthenticationError: on any malformed, mis-signed, expired or mis-scoped token.
    """
    parts = token.encode().split(b".")
    if len(parts) != 3:
        raise AuthenticationError("malformed token")
    header_b64, body_b64, signature_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
    except json.JSONDecodeError as exc:
        raise AuthenticationError("malformed token header") from exc
    # Allowlist, not a denylist: anything that is not exactly HS256 is rejected.
    if header.get("alg") != ALGORITHM:
        raise AuthenticationError("unsupported token algorithm")

    expected = _sign(header_b64 + b"." + body_b64, secret)
    if not hmac.compare_digest(_b64url_decode(signature_b64), expected):
        raise AuthenticationError("invalid token signature")

    try:
        payload = json.loads(_b64url_decode(body_b64))
    except json.JSONDecodeError as exc:
        raise AuthenticationError("malformed token payload") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("malformed token payload")

    now = int(datetime.now(UTC).timestamp())
    if (exp := payload.get("exp")) is not None and now > int(exp) + leeway_seconds:
        raise AuthenticationError("token has expired")
    if (nbf := payload.get("nbf")) is not None and now + leeway_seconds < int(nbf):
        raise AuthenticationError("token is not yet valid")
    if issuer is not None and payload.get("iss") != issuer:
        raise AuthenticationError("unexpected token issuer")
    if audience is not None:
        aud = payload.get("aud")
        allowed = aud if isinstance(aud, list) else [aud]
        if audience not in allowed:
            raise AuthenticationError("unexpected token audience")
    return payload


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    subject = claims.get("sub")
    raw_role = claims.get("role")
    if not subject or not raw_role:
        raise AuthenticationError("token is missing the 'sub' or 'role' claim")
    try:
        role = PrincipalRole(raw_role)
    except ValueError as exc:
        raise AuthenticationError(f"unknown role: {raw_role}") from exc
    try:
        subject_id = uuid.UUID(str(subject))
    except ValueError as exc:
        raise AuthenticationError("token 'sub' claim is not a UUID") from exc
    return Principal(id=subject_id, role=role, claims=claims)


# --- Agency API keys ----------------------------------------------------------------------


def hash_api_key(api_key: str) -> str:
    """SHA-256 hex digest of an agency API key.

    A plain digest (rather than a slow KDF) is the right primitive here: the key is a
    high-entropy random 256-bit value we generate, not a user-chosen password, so there is no
    guessable keyspace for an offline attack to exploit.
    """
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Return ``(plaintext_key, sha256_hash)``. The plaintext is shown to the agency once."""
    key = f"nwa_{secrets.token_urlsafe(_AGENCY_KEY_BYTES)}"
    return key, hash_api_key(key)
