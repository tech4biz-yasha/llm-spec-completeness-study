"""Write-once object storage for the NOC — rules.yaml#EXIT-09.

    "NOC is a PDF, stored in the UAE region bucket, immutable once issued."

Immutability is enforced twice: the store refuses to overwrite an existing key,
and the noc_documents row cannot be updated or deleted (DB trigger).
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Protocol

from ..config import UAE_REGIONS
from ..ports import StoredObject


class ObjectAlreadyExists(RuntimeError):
    """A NOC key was written twice. rules.yaml#EXIT-09 — immutable once issued."""


class S3ClientLike(Protocol):
    def put_object(self, **kwargs: Any) -> Any: ...

    def head_object(self, **kwargs: Any) -> Any: ...


class S3ObjectStore:
    """S3-compatible store, pinned to a UAE region (rules.yaml#EXIT-09).

    The client is injected; this module does not own AWS credentials. Writes use
    ``IfNoneMatch: '*'`` so a second write of the same key fails at the service
    rather than silently replacing an issued NOC.
    """

    def __init__(self, client: S3ClientLike, *, bucket: str, region: str) -> None:
        if region not in UAE_REGIONS:
            raise ValueError(
                f"NOC bucket region {region!r} is not in the UAE; rules.yaml#EXIT-09 "
                f"requires one of {sorted(UAE_REGIONS)}"
            )
        self._client = client
        self._bucket = bucket
        self._region = region

    async def put_immutable(self, key: str, body: bytes, content_type: str) -> StoredObject:
        digest = hashlib.sha256(body).hexdigest()

        def _put() -> None:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                ChecksumSHA256=_b64_sha256(body),
                IfNoneMatch="*",  # rules.yaml#EXIT-09 — never overwrite an issued NOC
            )

        await asyncio.to_thread(_put)
        return StoredObject(
            bucket=self._bucket,
            key=key,
            region=self._region,
            sha256=digest,
            size_bytes=len(body),
        )


class LocalImmutableObjectStore:
    """Filesystem store with the same write-once contract. Local runs and tests."""

    def __init__(self, root: Path, *, bucket: str, region: str) -> None:
        if region not in UAE_REGIONS:
            raise ValueError(f"NOC bucket region {region!r} is not in the UAE (rules.yaml#EXIT-09)")
        self._root = root
        self._bucket = bucket
        self._region = region

    async def put_immutable(self, key: str, body: bytes, content_type: str) -> StoredObject:
        path = self._root / self._bucket / key
        path.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            try:
                # 'xb' — exclusive create. rules.yaml#EXIT-09 immutability.
                with path.open("xb") as handle:
                    handle.write(body)
            except FileExistsError as exc:
                raise ObjectAlreadyExists(f"{self._bucket}/{key} already exists") from exc

        await asyncio.to_thread(_write)
        return StoredObject(
            bucket=self._bucket,
            key=key,
            region=self._region,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
        )


def _b64_sha256(body: bytes) -> str:
    import base64

    return base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
