"""Shared rendering and real MIME assembly, without contacting an SMTP server."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.email_templates import (
    branded_email,
    render_customer_notice,
    render_customer_reply,
    render_insurer_forward,
    render_staff_invitation,
)
from app.services.providers import SMTPDelivery


@pytest.mark.parametrize("locale", ["en", "ar"])
@pytest.mark.parametrize(
    "kind", ["created", "status_changed", "reply", "insurer", "invite", "reset"]
)
def test_all_templates_are_branded_and_escape_dynamic_content(locale: str, kind: str) -> None:
    unsafe = '<script>alert("unsafe")</script>'
    if kind in {"created", "status_changed"}:
        subject, body = render_customer_notice(kind, unsafe, "resolved", locale)
    elif kind == "reply":
        subject, body = render_customer_reply("CMP-42", unsafe, locale)
    elif kind == "insurer":
        subject, body = render_insurer_forward(
            ticket_no="CMP-42",
            subject=unsafe,
            details="Customer description",
            policy_number="POL-42",
            insurance_type="health",
            locale=locale,
            agent_note="Approved note",
        )
        assert "POL-42" in body
        assert "Customer description" in body
        assert "Approved note" in body
        if locale == "en":
            for field in [
                "Insurance type",
                "Summary",
                "Customer-visible description",
                "Agent note",
            ]:
                assert field in body
    else:
        subject, body = render_staff_invitation(
            'https://portal.example/invite#token=abc&value="quoted"', reset=kind == "reset"
        )
    text, html = branded_email(
        subject,
        body,
        brand="Acme & Partners",
        public_url="https://portal.example",
        support_email="help@portal.example",
        locale=locale,
    )
    assert subject
    assert body in text
    assert "Acme & Partners" in text
    assert "help@portal.example" in text
    assert "This is an automated message from Acme & Partners." in text
    assert "/api/public/brand/logo" not in html
    assert "<img" not in html  # No logo bytes -> text-only header, never a broken img.
    assert "Acme &amp; Partners" in html
    assert "help@portal.example" in html
    assert "<script>" not in html
    if unsafe in body:
        assert "&lt;script&gt;" in html
    if locale == "ar":
        assert 'lang="ar" dir="rtl"' in html
    if kind in {"invite", "reset"}:
        assert 'href="https://portal.example/invite#token=abc&amp;value=&quot;quoted&quot;"' in html


def test_missing_logo_has_text_header_and_unsafe_origin_is_not_linked() -> None:
    _, html = branded_email("Title", "Body", brand="Acme", public_url="javascript:alert(1)")
    assert "Acme" in html
    assert "<img" not in html
    assert "javascript:" not in html


@pytest.mark.parametrize(
    "kind", ["otp", "invite", "reset", "created", "status", "insurer", "report"]
)
async def test_every_smtp_path_has_both_parts(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr("app.services.providers.aiosmtplib.send", send)
    settings = SimpleNamespace(
        smtp_host="smtp.example.com",
        smtp_from="mail@example.com",
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_start_tls=True,
        smtp_use_tls=False,
        public_app_url="https://portal.example",
        support_email="help@example.com",
    )
    provider = SMTPDelivery(settings, AsyncMock(return_value="Acme"))
    if kind == "otp":
        await provider.send_otp("to@example.com", "123456")
    elif kind == "invite":
        await provider.send_invitation(
            "to@example.com", "https://portal.example/invite#token=secret"
        )
    elif kind == "reset":
        await provider.send_password_reset(
            "to@example.com", "https://portal.example/invite#token=secret"
        )
    elif kind == "insurer":
        await provider.send_insurer("to@example.com", "CMP-42", "Approved escalation")
    elif kind == "report":
        await provider.send_report(
            ["to@example.com"], "Report", "Summary", "report.csv", "text/csv", b"a,b"
        )
    else:
        await provider.send_notice(
            "not-in-email",
            "to@example.com",
            kind,
            {"ticket_no": "CMP-42", "status": "new", "locale": "ar"},
        )
    message = send.call_args.args[0]
    plain = message.get_body(preferencelist=("plain",)).get_content()
    html = message.get_body(preferencelist=("html",)).get_content()
    assert any(p.get_content_type() == "multipart/alternative" for p in message.walk())
    for body in [plain, html]:
        assert "Acme" in body
        assert "help@example.com" in body
        assert "This is an automated message" in body
        assert "not-in-email" not in body
    assert "/api/public/brand/logo" not in html
    assert "cid:" not in html
    if kind == "report":
        assert len(list(message.iter_attachments())) == 1


async def test_insurer_forward_excludes_identifiers_and_private_staff_notes() -> None:
    from uuid import uuid4

    from app.db import Actor
    from app.domain.enums import InsuranceType
    from app.domain.schemas import SendComplaintEmailRequest
    from app.security.integration_crypto import SecretCipher
    from app.services.integrations import IntegrationService

    complaint = SimpleNamespace(
        id=uuid4(),
        company_id=uuid4(),
        ticket_no="CMP-42",
        insurance_type=InsuranceType.HEALTH,
        policy_number="POL-42",
        subject="Claim delay 29901010112345",
        description="Customer description: call +201123456789; ID 29901010112345",
        internal_notes="PRIVATE STAFF NOTE MUST NOT BE SENT",
    )
    service = IntegrationService(SecretCipher(b"p" * 32), b"s" * 32)
    service.complaints.get = AsyncMock(return_value=complaint)
    service.repository.company_contacts = AsyncMock(return_value=["claims@example.com"])
    service._queue_message = AsyncMock(return_value={})
    await service.send_complaint_by_email(
        SimpleNamespace(),
        SimpleNamespace(actor=Actor(uuid4(), "staff")),
        complaint.id,
        SendComplaintEmailRequest(locale="en", agent_note="Approved note +201123456789"),
        "safe-forward-key",
    )
    payload = service._queue_message.call_args.kwargs["payload"]
    text, html = branded_email(payload["subject"], payload["body"])
    for value in [payload["subject"], text, html]:
        for forbidden in ["29901010112345", "+201123456789", complaint.internal_notes]:
            assert forbidden not in value
    for field in ["CMP-42", "POL-42", "health", "Customer description", "Approved note"]:
        assert field in text and field in html


# --- CID logo embedding (Part 1) -------------------------------------------------

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_branded_email_uses_cid_logo_only_when_logo_bytes_exist() -> None:
    from app.services.brand_assets import LOGO_CID_REF

    _, html = branded_email(
        "Title", "Body", brand="Acme", public_url="https://portal.example", has_logo=True
    )
    assert f'src="{LOGO_CID_REF}"' in html
    assert "/api/public/brand/logo" not in html
    assert "http" not in html.split('src="cid:logo@brand"')[0].rsplit("<img", 1)[-1]

    _, html_without = branded_email(
        "Title", "Body", brand="Acme", public_url="https://portal.example"
    )
    assert "<img" not in html_without
    assert "cid:" not in html_without


async def test_smtp_message_embeds_logo_as_inline_cid_part(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.brand_assets import BrandLogo

    send = AsyncMock()
    monkeypatch.setattr("app.services.providers.aiosmtplib.send", send)
    settings = SimpleNamespace(
        smtp_host="smtp.example.com",
        smtp_from="mail@example.com",
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_start_tls=True,
        smtp_use_tls=False,
        public_app_url="https://portal.example",
        support_email="help@example.com",
    )
    provider = SMTPDelivery(
        settings,
        AsyncMock(return_value="Acme"),
        logo_loader=AsyncMock(return_value=BrandLogo(PNG_BYTES, "image/png")),
    )
    await provider.send_otp("to@example.com", "123456")
    message = send.call_args.args[0]
    html = message.get_body(preferencelist=("html",)).get_content()
    assert 'src="cid:logo@brand"' in html
    assert "/api/public/brand/logo" not in html
    images = [
        part
        for part in message.walk()
        if part.get_content_type() == "image/png"
    ]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<logo@brand>"
    assert images[0].get_content_disposition() == "inline"
    assert images[0].get_payload(decode=True) == PNG_BYTES
    plain = message.get_body(preferencelist=("plain",)).get_content()
    assert "cid:" not in plain


async def test_smtp_message_without_logo_has_no_image_part(monkeypatch: pytest.MonkeyPatch) -> None:
    send = AsyncMock()
    monkeypatch.setattr("app.services.providers.aiosmtplib.send", send)
    settings = SimpleNamespace(
        smtp_host="smtp.example.com",
        smtp_from="mail@example.com",
        smtp_port=587,
        smtp_username=None,
        smtp_password=None,
        smtp_start_tls=True,
        smtp_use_tls=False,
        public_app_url="https://portal.example",
        support_email="help@example.com",
    )
    provider = SMTPDelivery(
        settings, AsyncMock(return_value="Acme"), logo_loader=AsyncMock(return_value=None)
    )
    await provider.send_invitation("to@example.com", "https://portal.example/invite#token=x")
    message = send.call_args.args[0]
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "<img" not in html
    assert not [part for part in message.walk() if part.get_content_maintype() == "image"]


def test_detect_logo_rejects_mislabelled_or_unknown_bytes() -> None:
    from app.services.brand_assets import coerce_logo, detect_logo

    assert detect_logo(PNG_BYTES, "image/png") is not None
    assert detect_logo(PNG_BYTES, "image/jpeg") is None
    assert detect_logo(b"not an image", "image/png") is None
    assert detect_logo(b"", "image/png") is None
    assert coerce_logo((PNG_BYTES, "image/png")).content == PNG_BYTES  # type: ignore[union-attr]
    assert coerce_logo(None) is None
