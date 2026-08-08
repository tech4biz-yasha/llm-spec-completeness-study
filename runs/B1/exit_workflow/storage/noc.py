"""NOC object storage.

rules.yaml#EXIT-09: "NOC is a PDF, stored in the UAE region bucket, immutable
once issued, linked on the workflow."

Two properties are checked here rather than taken on trust:

* **UAE region.** The configured region is checked against :data:`UAE_REGIONS`
  and a mismatch is a startup failure, not a warning. A NOC written to the wrong
  region is a data-residency incident that no later code can undo.
* **Write-once.** Both backends refuse to overwrite an existing object. The S3
  backend additionally relies on the bucket's Object Lock policy; the
  precondition here is the second line of defence, not the only one.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from exit_workflow.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: AWS region in the United Arab Emirates.
UAE_REGIONS: Final[frozenset[str]] = frozenset({"me-central-1"})

CONTENT_TYPE: Final[str] = "application/pdf"


class NocStorageError(RuntimeError):
    """The NOC could not be stored."""


class ImmutableObjectExists(NocStorageError):
    """An object already exists at this key and may not be replaced."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Where a NOC came to rest, and what it was."""

    bucket: str
    region: str
    key: str
    sha256: str
    byte_size: int


class NocStorage(Protocol):
    """Write-once object storage for issued NOCs."""

    bucket: str
    region: str

    async def put_immutable(self, key: str, content: bytes) -> StoredObject:
        """Store ``content`` at ``key``, refusing to replace an existing object."""

    async def get(self, key: str) -> bytes | None:
        """Read an object back, for verification and for serving the document."""


def _validate_region(region: str) -> str:
    if region not in UAE_REGIONS:
        raise NocStorageError(
            f"NOC region {region!r} is not a UAE region; rules.yaml#EXIT-09 requires the "
            f"UAE region bucket (permitted: {sorted(UAE_REGIONS)})"
        )
    return region


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class S3NocStorage:
    """S3 backend with a write-once precondition."""

    def __init__(self, bucket: str, region: str, *, client: Any | None = None) -> None:
        self.bucket = bucket
        self.region = _validate_region(region)
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import boto3  # noqa: PLC0415 - optional dependency, resolved on first use
            except ImportError as exc:  # pragma: no cover - deployment concern
                raise NocStorageError(
                    "boto3 is required for the S3 NOC storage backend; install it or set "
                    "EXIT_NOC_STORAGE_BACKEND=filesystem"
                ) from exc
            self._client = boto3.client("s3", region_name=self.region)
        return self._client

    async def put_immutable(self, key: str, content: bytes) -> StoredObject:
        import asyncio

        client = self._get_client()

        def _put() -> None:
            # IfNoneMatch='*' fails the request if the key already exists, so a
            # retried issuance cannot overwrite an issued NOC.
            client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=content,
                ContentType=CONTENT_TYPE,
                IfNoneMatch="*",
                ChecksumSHA256=hashlib.sha256(content).digest().hex(),
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:  # botocore exception types are created dynamically
            if type(exc).__name__ in {"PreconditionFailed", "ConditionalRequestConflict"} or (
                getattr(exc, "response", {}).get("Error", {}).get("Code")
                in {"PreconditionFailed", "ConditionalRequestConflict"}
            ):
                raise ImmutableObjectExists(
                    f"s3://{self.bucket}/{key} already exists and is immutable"
                ) from exc
            raise NocStorageError(f"failed to store s3://{self.bucket}/{key}: {exc}") from exc

        return StoredObject(
            bucket=self.bucket,
            region=self.region,
            key=key,
            sha256=digest(content),
            byte_size=len(content),
        )

    async def get(self, key: str) -> bytes | None:
        import asyncio

        client = self._get_client()

        def _get() -> bytes | None:
            try:
                response = client.get_object(Bucket=self.bucket, Key=key)
            except Exception as exc:
                if getattr(exc, "response", {}).get("Error", {}).get("Code") in {
                    "NoSuchKey",
                    "404",
                }:
                    return None
                raise
            return response["Body"].read()

        return await asyncio.to_thread(_get)


class FilesystemNocStorage:
    """Local write-once storage for development and tests.

    Files are created with ``O_EXCL`` and then made read-only, so the write-once
    guarantee holds against this module itself rather than resting on
    convention.
    """

    def __init__(self, root: str | Path, bucket: str, region: str) -> None:
        self.bucket = bucket
        self.region = _validate_region(region)
        self._root = Path(root) / bucket
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root.resolve()):
            raise NocStorageError(f"object key {key!r} escapes the storage root")
        return candidate

    async def put_immutable(self, key: str, content: bytes) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError as exc:
            raise ImmutableObjectExists(f"{path} already exists and is immutable") from exc
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return StoredObject(
            bucket=self.bucket,
            region=self.region,
            key=key,
            sha256=digest(content),
            byte_size=len(content),
        )

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None


def build_storage(settings: Settings | None = None) -> NocStorage:
    """Construct the configured NOC storage backend."""
    settings = settings or get_settings()
    if settings.noc_storage_backend == "filesystem":
        if settings.is_production:
            raise NocStorageError(
                "the filesystem NOC backend is not permitted in production; "
                "rules.yaml#EXIT-09 requires the UAE region bucket"
            )
        return FilesystemNocStorage(
            settings.noc_filesystem_root, settings.noc_bucket, settings.noc_region
        )
    return S3NocStorage(settings.noc_bucket, settings.noc_region)
