"""Private S3 object storage; uploads/downloads are proxied through the scoped API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Protocol

import boto3
from botocore.config import Config


@dataclass(frozen=True)
class StorageObject:
    """Safe S3 listing metadata; object bodies and credentials never leave the service."""

    key: str
    size_bytes: int
    last_modified: datetime | None
    etag: str | None


@dataclass(frozen=True)
class StorageObjectPage:
    """One delimiter-aware, bounded page of a private bucket inventory."""

    folders: tuple[str, ...]
    objects: tuple[StorageObject, ...]
    is_truncated: bool
    next_token: str | None


class StorageConfig(Protocol):
    s3_bucket: str
    s3_endpoint_url: str | None
    s3_access_key_id: str | None
    s3_secret_access_key: str | None
    s3_region: str


class ObjectStorage(Protocol):
    async def put(self, key: str, file: BinaryIO, mime: str) -> None: ...
    async def get(self, key: str, max_bytes: int) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def list_objects(
        self, prefix: str, continuation_token: str | None, limit: int
    ) -> StorageObjectPage: ...


class S3ObjectStorage:
    def __init__(self, settings: StorageConfig) -> None:
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def put(self, key: str, file: BinaryIO, mime: str) -> None:
        file.seek(0)
        await asyncio.to_thread(
            self.client.upload_fileobj, file, self.bucket, key, ExtraArgs={"ContentType": mime}
        )

    async def get(self, key: str, max_bytes: int) -> bytes:
        def read() -> bytes:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                with SpooledTemporaryFile(max_size=max_bytes) as buffer:
                    while chunk := body.read(65_536):
                        if buffer.tell() + len(chunk) > max_bytes:
                            raise ValueError("Object exceeds permitted size")
                        buffer.write(chunk)
                    buffer.seek(0)
                    return buffer.read()
            finally:
                body.close()

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def list_objects(
        self, prefix: str, continuation_token: str | None, limit: int
    ) -> StorageObjectPage:
        """List one bounded folder-aware page without loading object contents."""
        if not 1 <= limit <= 500:
            raise ValueError("Storage page limit must be between 1 and 500")

        def read() -> StorageObjectPage:
            params: dict[str, object] = {
                "Bucket": self.bucket,
                "Prefix": prefix,
                "Delimiter": "/",
                "MaxKeys": limit,
            }
            if continuation_token:
                params["ContinuationToken"] = continuation_token
            response = self.client.list_objects_v2(**params)

            folders = tuple(
                sorted(
                    {
                        item["Prefix"]
                        for item in response.get("CommonPrefixes", [])
                        if isinstance(item.get("Prefix"), str)
                        and item["Prefix"].startswith(prefix)
                        and item["Prefix"] != prefix
                    }
                )
            )
            objects: list[StorageObject] = []
            for item in response.get("Contents", []):
                key = item.get("Key")
                if not isinstance(key, str) or not key.startswith(prefix):
                    continue
                # S3-compatible services may represent an empty folder as a marker object.
                # The delimiter response already exposes that marker as a CommonPrefix.
                if key == prefix or key.endswith("/"):
                    continue
                last_modified = item.get("LastModified")
                objects.append(
                    StorageObject(
                        key=key,
                        size_bytes=max(0, int(item.get("Size", 0))),
                        last_modified=(
                            last_modified if isinstance(last_modified, datetime) else None
                        ),
                        etag=(item.get("ETag") if isinstance(item.get("ETag"), str) else None),
                    )
                )

            truncated = response.get("IsTruncated") is True
            next_token = response.get("NextContinuationToken")
            if truncated and not isinstance(next_token, str):
                raise RuntimeError("S3 response omitted its continuation token")
            return StorageObjectPage(
                folders=folders,
                objects=tuple(objects),
                is_truncated=truncated,
                next_token=next_token if isinstance(next_token, str) else None,
            )

        return await asyncio.to_thread(read)
