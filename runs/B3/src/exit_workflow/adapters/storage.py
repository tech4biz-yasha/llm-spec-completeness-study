"""Object storage adapters. rules.yaml#EXIT-09 — UAE region bucket, immutable."""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from ..ports.storage import StorageError, StoredObject


class InMemoryObjectStorage:
    """Write-once in-process store. Used by tests and local development."""

    def __init__(self, region: str = "me-central-1") -> None:
        self._region = region
        self.objects: dict[tuple[str, str], bytes] = {}

    @property
    def region(self) -> str:
        return self._region

    def put_immutable(
        self, *, bucket: str, key: str, body: bytes, content_type: str
    ) -> StoredObject:
        if (bucket, key) in self.objects:
            # rules.yaml#EXIT-09 — immutable once issued.
            raise StorageError(f"object {bucket}/{key} already exists and is immutable")
        self.objects[(bucket, key)] = body
        return StoredObject(
            bucket=bucket,
            key=key,
            region=self._region,
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
        )


class S3ObjectStorage:
    """S3-compatible adapter pinned to a UAE region with object-lock semantics.

    ``boto3`` is an optional extra. Immutability is asserted two ways: the write uses
    ``IfNoneMatch: '*'`` so a second write to the same key fails at the service, and the
    bucket is expected to carry an Object Lock retention policy — an application-side
    check alone would not satisfy "immutable once issued".
    """

    def __init__(self, *, region: str, client: Any | None = None) -> None:
        self._region = region
        if client is not None:
            self._client = client
            return
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise RuntimeError(
                "S3ObjectStorage requires the 's3' extra: pip install exit-workflow[s3]"
            ) from exc
        self._client = boto3.client("s3", region_name=region)

    @property
    def region(self) -> str:
        return self._region

    def put_immutable(
        self, *, bucket: str, key: str, body: bytes, content_type: str
    ) -> StoredObject:  # pragma: no cover - requires S3
        digest = hashlib.sha256(body).hexdigest()
        try:
            self._client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
                ChecksumSHA256=base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"),
                Metadata={"sha256": digest},
            )
        except Exception as exc:
            raise StorageError(f"failed to store {bucket}/{key}: {exc}") from exc
        return StoredObject(
            bucket=bucket, key=key, region=self._region, size_bytes=len(body), sha256=digest
        )
