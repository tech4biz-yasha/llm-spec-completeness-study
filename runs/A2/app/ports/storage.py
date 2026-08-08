"""Document blob storage port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredObject:
    storage_key: str
    size_bytes: int
    checksum_sha256: str
    content_type: str


class DocumentStorage(Protocol):
    """Content-addressed blob store for exit documents and rendered NOCs."""

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Store ``data`` at ``key``. Overwriting an existing key is an error."""
        ...

    async def get(self, key: str) -> bytes:
        """Fetch the bytes at ``key``. Raises :class:`DocumentNotStoredError` if absent."""
        ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def presigned_url(self, key: str, *, expires_in_seconds: int = 300) -> str | None:
        """A short-lived direct download URL, or ``None`` if the backend has none."""
        ...


class DocumentNotStoredError(RuntimeError):
    """The requested object is not present in the blob store."""
