from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

from app.api.platform import upload_platform_logo


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
