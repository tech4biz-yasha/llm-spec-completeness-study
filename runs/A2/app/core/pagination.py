"""Pagination primitives.

Listing endpoints use keyset (cursor) pagination so p95 latency stays flat as the
workflow table grows -- SRS §5.1 requires sub-200ms p95 and OFFSET-based paging
degrades linearly with offset depth.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.errors import ValidationFailedError

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class Cursor:
    """Opaque keyset cursor over ``(created_at DESC, id DESC)``."""

    created_at: datetime
    id: UUID

    def encode(self) -> str:
        raw = json.dumps({"c": self.created_at.isoformat(), "i": str(self.id)})
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, token: str) -> Cursor:
        try:
            padded = token + "=" * (-len(token) % 4)
            data = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
            return cls(created_at=datetime.fromisoformat(data["c"]), id=UUID(data["i"]))
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise ValidationFailedError(
                "Malformed pagination cursor.", details={"field": "cursor"}
            ) from exc


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = Field(
        default=None, description="Pass back as `cursor` to fetch the next page."
    )
    has_more: bool = False


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if limit < 1:
        raise ValidationFailedError("limit must be >= 1", details={"field": "limit"})
    return min(limit, MAX_PAGE_SIZE)
