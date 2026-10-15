"""Private S3 object storage; uploads/downloads are proxied through the scoped API."""

from __future__ import annotations

import asyncio
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Protocol

import boto3
from botocore.config import Config


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
