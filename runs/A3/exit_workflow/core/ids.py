"""Human-facing identifier generation.

T13 step 4 requires a "Workflow ID" that is quoted in owner and inspection
agency emails, so it must be short, unambiguous when read aloud or re-typed,
and unguessable enough that it is not an enumeration oracle.
"""

from __future__ import annotations

import secrets

# Crockford-style alphabet: no I, L, O, U — avoids 1/I and 0/O confusion.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _random_block(length: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def workflow_reference(prefix: str, year: int) -> str:
    """e.g. ``EXW-2026-7K3M9Q``."""

    return f"{prefix}-{year}-{_random_block(6)}"


def noc_number(prefix: str, year: int) -> str:
    """e.g. ``NOC-2026-4T8XB2``."""

    return f"{prefix}-{year}-{_random_block(6)}"


def verification_code() -> str:
    """Opaque code embedded in the NOC for third-party verification."""

    return _random_block(12)


def request_reference(prefix: str = "INSP") -> str:
    return f"{prefix}-{_random_block(8)}"
