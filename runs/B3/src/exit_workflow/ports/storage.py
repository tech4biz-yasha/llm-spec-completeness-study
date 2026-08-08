"""Object storage port. rules.yaml#EXIT-09 — UAE region bucket, immutable once issued."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredObject:
    bucket: str
    key: str
    region: str
    size_bytes: int
    sha256: str


@runtime_checkable
class ObjectStorage(Protocol):
    @property
    def region(self) -> str:
        """Region the bucket lives in; validated as a UAE region at start-up."""

    def put_immutable(
        self, *, bucket: str, key: str, body: bytes, content_type: str
    ) -> StoredObject:
        """Write once. Implementations must reject an overwrite of an existing key
        (rules.yaml#EXIT-09, "immutable once issued")."""
