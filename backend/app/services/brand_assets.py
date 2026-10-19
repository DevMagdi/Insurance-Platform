"""Inline (CID) brand logo for outbound email.

Most email clients block remote images, and a logo served from
``/api/public/brand/logo`` may additionally be unreachable (localhost,
cookie-gated, or behind a proxy) for the recipient. Every branded HTML
email therefore embeds the logo bytes as a MIME ``multipart/related``
image part referenced with ``cid:logo@brand`` so the mark renders without
any network fetch. When no logo is configured, callers render a text-only
header — never a broken ``<img>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any

LOGO_CID = "logo@brand"
LOGO_CID_REF = f"cid:{LOGO_CID}"

_MIME_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

_EXTENSION = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}


@dataclass(frozen=True)
class BrandLogo:
    """Validated raster logo bytes ready to embed in an email."""

    content: bytes = field(repr=False)
    mime_type: str = "image/png"

    @property
    def subtype(self) -> str:
        return self.mime_type.partition("/")[2] or "png"

    @property
    def filename(self) -> str:
        return f"logo.{_EXTENSION.get(self.mime_type, 'png')}"


def detect_logo(content: bytes | None, mime_type: str | None = None) -> BrandLogo | None:
    """Return a :class:`BrandLogo` for supported raster bytes, else ``None``.

    The declared/stored MIME type is only trusted when the magic bytes
    agree with it, so a corrupt or mislabelled row degrades to the
    text-only header instead of shipping a broken image part.
    """
    if not content:
        return None
    detected: str | None = None
    for magic, mime in _MIME_BY_MAGIC:
        if content.startswith(magic):
            detected = mime
            break
    if (
        detected is None
        and len(content) >= 12
        and content[:4] == b"RIFF"
        and content[8:12] == b"WEBP"
    ):
        detected = "image/webp"
    if detected is None:
        return None
    if mime_type and mime_type != detected:
        return None
    return BrandLogo(content=bytes(content), mime_type=detected)


def coerce_logo(value: Any) -> BrandLogo | None:
    """Normalise loader results (``BrandLogo`` | ``(bytes, mime)`` | bytes)."""
    if value is None or isinstance(value, BrandLogo):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return detect_logo(value[0], value[1] if isinstance(value[1], str) else None)
    if isinstance(value, (bytes, bytearray)):
        return detect_logo(bytes(value))
    return None


def attach_inline_logo(message: EmailMessage, logo: BrandLogo | None) -> bool:
    """Attach ``logo`` as an inline CID image part; no-op without a logo.

    The HTML part is wrapped in ``multipart/related`` (text stays the first
    alternative, so plain-text clients are unaffected) and the image is
    appended as its inline sibling, referenced by ``cid:logo@brand``.
    """
    if logo is None:
        return False
    html_part: EmailMessage | None = None
    if message.get_content_type() == "text/html":
        html_part = message
    else:
        for part in message.walk():
            if part.get_content_type() == "text/html":
                html_part = part
                break
    if html_part is None:
        return False
    image = EmailMessage()
    image.set_content(
        logo.content,
        maintype="image",
        subtype=logo.subtype,
        disposition="inline",
        filename=logo.filename,
    )
    image["Content-ID"] = f"<{LOGO_CID}>"
    html_part.make_related()
    html_part.attach(image)
    return True
