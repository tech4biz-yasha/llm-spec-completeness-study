"""Translating database constraint violations into domain errors.

Uniqueness that the kit treats as a business rule (one workflow per contract,
one payment per workflow) is enforced by PostgreSQL, so the violation arrives as
an :class:`~sqlalchemy.exc.IntegrityError` and has to be mapped back to the rule
it broke.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def constraint_name(error: IntegrityError) -> str | None:
    """Best-effort extraction of the violated constraint name.

    asyncpg exposes ``constraint_name`` on its exception; SQLAlchemy's DBAPI
    shim wraps it, so the attribute is looked for on both the wrapper and its
    cause before falling back to the message text.
    """
    original = error.orig
    for candidate in (original, getattr(original, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None


def violates(error: IntegrityError, name: str) -> bool:
    """True when ``error`` reports a violation of the named constraint."""
    reported = constraint_name(error)
    if reported is not None:
        return reported == name
    return name in str(error.orig)
