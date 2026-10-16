"""The object-store bootstrap is idempotent, private by default and waits for the endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError

from app.scripts import init_bucket

ROOT = Path(__file__).resolve().parents[2]


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class FakeS3:
    def __init__(self, create_errors: list[Exception]) -> None:
        self.create_errors = create_errors
        self.calls: list[str] = []

    def create_bucket(self, Bucket: str) -> None:
        self.calls.append(f"create:{Bucket}")
        if self.create_errors:
            raise self.create_errors.pop(0)

    def delete_bucket_policy(self, Bucket: str) -> None:
        self.calls.append("delete_policy")
        raise _client_error("NoSuchBucketPolicy")

    def head_bucket(self, Bucket: str) -> dict[str, Any]:
        self.calls.append("head")
        return {}


def test_creates_bucket_after_endpoint_comes_up() -> None:
    fake = FakeS3([EndpointConnectionError(endpoint_url="http://object-store:9000")])
    init_bucket.ensure_bucket(fake, "attachments", attempts=3, delay=0)
    assert fake.calls == ["create:attachments", "create:attachments", "delete_policy", "head"]


def test_existing_bucket_is_not_an_error() -> None:
    fake = FakeS3([_client_error("BucketAlreadyOwnedByYou")])
    init_bucket.ensure_bucket(fake, "attachments", attempts=1, delay=0)
    assert fake.calls[-1] == "head"


def test_gives_up_with_a_clear_error() -> None:
    fake = FakeS3([_client_error("AccessDenied")] * 2)
    with pytest.raises(SystemExit, match="object store not ready"):
        init_bucket.ensure_bucket(fake, "attachments", attempts=2, delay=0)


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_stacks_use_maintained_object_store_images(name: str) -> None:
    services = yaml.safe_load((ROOT / name).read_text())["services"]
    store = services["object-store"]
    assert store["image"].startswith("chrislusf/seaweedfs:")
    assert store["environment"]["AWS_ACCESS_KEY_ID"].startswith("${S3_ACCESS_KEY_ID:")
    assert store["environment"]["AWS_SECRET_ACCESS_KEY"].startswith("${S3_SECRET_ACCESS_KEY:")
    assert services["object-store-init"]["command"] == ["python", "-m", "app.scripts.init_bucket"]
    for process in ("api", "worker"):
        assert services[process]["environment"]["S3_ENDPOINT_URL"] == "http://object-store:9000"
