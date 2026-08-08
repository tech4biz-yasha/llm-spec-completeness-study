"""Filesystem-backed document storage (local development, CI, single-node deployments)."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

from app.ports.storage import DocumentNotStoredError, DocumentStorage, StoredObject


class LocalDocumentStorage(DocumentStorage):
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Storage keys are built by us, but treat them as untrusted anyway: resolve and
        # confirm the result stays under the root so a crafted key cannot escape it.
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError(f"storage key escapes the storage root: {key!r}")
        return candidate

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL: storage keys embed a UUID, so a collision means a bug, not a retry.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(_write)
        return StoredObject(
            storage_key=key,
            size_bytes=len(data),
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise DocumentNotStoredError(key) from exc

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def presigned_url(self, key: str, *, expires_in_seconds: int = 300) -> str | None:
        # No direct-download URL: the API streams these through its own download route.
        return None
