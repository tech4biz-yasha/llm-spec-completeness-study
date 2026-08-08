"""Document storage port plus a local-filesystem adapter.

Production deployments bind :class:`DocumentStorage` to object storage (S3 or
equivalent); nothing above this module knows the difference.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Protocol

import anyio

from exit_workflow.core.errors import NotFoundError, StorageError, ValidationError

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,500}$")
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._\- ]")


def sanitize_filename(filename: str) -> str:
    """Strip directory components and anything that is not clearly benign."""

    base = os.path.basename(filename or "").strip()
    base = _UNSAFE_FILENAME_CHARS.sub("_", base).strip("._ ")
    if not base:
        base = "upload.bin"
    return base[:180]


def build_key(prefix: str, workflow_id: uuid.UUID, filename: str) -> str:
    return f"{prefix}/{workflow_id}/{uuid.uuid4().hex}-{sanitize_filename(filename)}"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DocumentStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class LocalFileStorage:
    """Filesystem adapter. Blocking I/O is pushed to a worker thread."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        if not _SAFE_KEY.match(key) or ".." in key:
            raise ValidationError(f"Unsafe storage key: {key!r}")
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):  # pragma: no cover - defensive
            raise ValidationError("Storage key escapes the storage root.")
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path_for(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)

        try:
            await anyio.to_thread.run_sync(_write)
        except OSError as exc:  # pragma: no cover - environment dependent
            raise StorageError(f"Could not store document {key!r}.") from exc

    async def get(self, key: str) -> bytes:
        path = self._path_for(key)
        try:
            return await anyio.to_thread.run_sync(path.read_bytes)
        except FileNotFoundError as exc:
            raise NotFoundError("Stored document is no longer available.") from exc
        except OSError as exc:  # pragma: no cover - environment dependent
            raise StorageError(f"Could not read document {key!r}.") from exc

    async def delete(self, key: str) -> None:
        path = self._path_for(key)

        def _unlink() -> None:
            path.unlink(missing_ok=True)

        await anyio.to_thread.run_sync(_unlink)

    def purge_all(self) -> None:  # pragma: no cover - test helper
        shutil.rmtree(self._root, ignore_errors=True)
        self._root.mkdir(parents=True, exist_ok=True)


class InMemoryStorage:
    """Used by tests and by ``EXITWF_ENVIRONMENT=test``."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self._blobs[key] = data

    async def get(self, key: str) -> bytes:
        try:
            return self._blobs[key]
        except KeyError as exc:
            raise NotFoundError("Stored document is no longer available.") from exc

    async def delete(self, key: str) -> None:
        self._blobs.pop(key, None)
