"""JSON coercion shared by the audit log, the outbox and API problem bodies."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def jsonable(value: Any) -> Any:
    """Coerce Decimals, UUIDs, dates and enums into JSON-safe primitives.

    Decimals become strings, never floats: a JSONB round-trip must not change
    a money value.
    """

    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if isinstance(value, Enum):
        return jsonable(value.value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):  # pragma: no cover - not expected in payloads
        return value.decode("utf-8", errors="replace")
    return value
