"""S3-compatible document storage.

Requires the ``s3`` extra (``pip install meridian-exit-workflow[s3]``). Objects are written
with server-side encryption and a checksum so integrity is verifiable at rest as well as
at download time.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.ports.storage import DocumentNotStoredError, DocumentStorage, StoredObject


class S3DocumentStorage(DocumentStorage):
    def __init__(
        self,
        *,
        bucket: str,
        region: str | None = None,
        endpoint_url: str | None = None,
        session: Any | None = None,
        sse: str = "AES256",
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url
        self._sse = sse
        self._session = session

    def _client(self) -> Any:
        if self._session is None:
            try:
                import aioboto3  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError(
                    "S3 storage requires the 's3' extra: pip install "
                    "'meridian-exit-workflow[s3]'"
                ) from exc
            self._session = aioboto3.Session()
        return self._session.client(
            "s3", region_name=self._region, endpoint_url=self._endpoint_url
        )

    async def put(
        self,
        *,
        key: str,
        data: bytes,
        content_type: str,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        async with self._client() as client:
            await client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ServerSideEncryption=self._sse,
                Metadata={**(metadata or {}), "sha256": digest},
            )
        return StoredObject(
            storage_key=key,
            size_bytes=len(data),
            checksum_sha256=digest,
            content_type=content_type,
        )

    async def get(self, key: str) -> bytes:
        async with self._client() as client:
            try:
                response = await client.get_object(Bucket=self._bucket, Key=key)
            except Exception as exc:  # noqa: BLE001 - botocore raises dynamic classes
                if type(exc).__name__ in {"NoSuchKey", "ClientError", "404"}:
                    raise DocumentNotStoredError(key) from exc
                raise
            async with response["Body"] as stream:
                data: bytes = await stream.read()
            return data

    async def delete(self, key: str) -> None:
        async with self._client() as client:
            await client.delete_object(Bucket=self._bucket, Key=key)

    async def exists(self, key: str) -> bool:
        async with self._client() as client:
            try:
                await client.head_object(Bucket=self._bucket, Key=key)
            except Exception:  # noqa: BLE001
                return False
            return True

    async def presigned_url(self, key: str, *, expires_in_seconds: int = 300) -> str | None:
        async with self._client() as client:
            url: str = await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in_seconds,
            )
            return url
