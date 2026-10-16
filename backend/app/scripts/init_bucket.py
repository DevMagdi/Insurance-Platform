"""Create the private attachments bucket on a self-hosted S3 endpoint (idempotent).

Used by the ``object-store-init`` one-shot service in the compose stacks.
It only talks the S3 API, so it works against SeaweedFS or any compatible
store. Managed S3 deployments create the bucket with infrastructure-as-code instead.

Environment: ``S3_ENDPOINT_URL``, ``S3_ACCESS_KEY_ID``, ``S3_SECRET_ACCESS_KEY``,
``S3_BUCKET`` and optionally ``S3_REGION`` (default ``us-east-1``).
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

_ALREADY_EXISTS = {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}


def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name=os.getenv("S3_REGION", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 1},
        ),
    )


def ensure_bucket(client: Any, bucket: str, attempts: int = 45, delay: float = 2.0) -> None:
    """Create ``bucket`` if missing, waiting for the endpoint to come up."""
    for attempt in range(1, attempts + 1):
        try:
            client.create_bucket(Bucket=bucket)
            print(f"created bucket {bucket}")
            break
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _ALREADY_EXISTS:
                print(f"bucket {bucket} already exists")
                break
            error: Exception = exc
        except BotoCoreError as exc:  # endpoint not reachable yet
            error = exc
        if attempt == attempts:
            raise SystemExit(f"object store not ready: {error}")
        time.sleep(delay)
    # Private by default: drop any bucket policy (equivalent of `mc anonymous set none`).
    # (No policy set, or a store without bucket policies, is fine.)
    with contextlib.suppress(ClientError):
        client.delete_bucket_policy(Bucket=bucket)
    client.head_bucket(Bucket=bucket)


def main() -> int:
    ensure_bucket(_client(), os.environ["S3_BUCKET"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
