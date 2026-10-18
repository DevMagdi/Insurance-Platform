from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image
from pydantic import ValidationError
from starlette.requests import Request

from app.api.platform import _public_platform_brand, update_brand, upload_platform_logo
from app.domain.schemas import PlatformBrandRequest


def make_request(body: bytes) -> Request:
    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/platform/brand/logo",
            "raw_path": b"/api/platform/brand/logo",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("example.test", 443),
        },
        receive,
    )


@pytest.mark.asyncio
async def test_public_brand_uses_default_identity_when_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenTransaction:
        async def __aenter__(self) -> None:
            raise RuntimeError("database unavailable")

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("app.api.platform.purpose_transaction", lambda *_: BrokenTransaction())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(factory=object())))
    assert await _public_platform_brand(request) == {
        "platform_name": "Insurance",
        "logo_url": "/insurance-logo.svg",
    }


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (2, 2), (18, 52, 86, 255)).save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_platform_logo_upload_accepts_valid_raster_and_persists_it() -> None:
    content = png_bytes()
    db = SimpleNamespace(scalar=AsyncMock(return_value=True))
    result = await upload_platform_logo(make_request(content), SimpleNamespace(db=db), None)
    assert result == {"logo_url": "/api/public/brand/logo"}
    query, parameters = db.scalar.await_args.args
    assert "update_platform_logo" in str(query)
    assert parameters == {"mime": "image/png", "content": content}


@pytest.mark.asyncio
async def test_platform_logo_upload_rejects_non_image_and_oversized_content() -> None:
    db = SimpleNamespace(scalar=AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as invalid:
        await upload_platform_logo(
            make_request(b"<svg onload='alert(1)'></svg>"), SimpleNamespace(db=db), None
        )
    assert invalid.value.status_code == 415
    with pytest.raises(HTTPException) as oversized:
        await upload_platform_logo(
            make_request(b"x" * (2 * 1024 * 1024 + 1)), SimpleNamespace(db=db), None
        )
    assert oversized.value.status_code == 413
    db.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_brand_persists_name_and_logo_from_the_strict_contract() -> None:
    db = SimpleNamespace(scalar=AsyncMock(return_value=True))
    item = PlatformBrandRequest(platform_name="  My Platform  ", logo_url="/insurance-logo.svg")
    result = await update_brand(item, SimpleNamespace(db=db), None)
    assert result == {"platform_name": "My Platform", "logo_url": "/insurance-logo.svg"}
    query, parameters = db.scalar.await_args.args
    assert "update_platform_brand" in str(query)
    assert parameters == {"name": "My Platform", "logo": "/insurance-logo.svg"}


def test_platform_brand_request_rejects_public_config_runtime_fields() -> None:
    """`captcha` and `privacy_policy_version` belong to /api/public/config only.

    The brand contract stays strict (extra="forbid"); clients must send exactly
    `platform_name` and `logo_url` — echoing unrelated runtime settings back is
    what caused the 422 on `PUT /api/platform/brand`.
    """
    with pytest.raises(ValidationError) as excinfo:
        PlatformBrandRequest(
            platform_name="My Platform",
            logo_url="/insurance-logo.svg",
            captcha=None,  # type: ignore[call-arg]
            privacy_policy_version="2026-10",  # type: ignore[call-arg]
        )
    message = str(excinfo.value)
    assert "captcha" in message
    assert "privacy_policy_version" in message
