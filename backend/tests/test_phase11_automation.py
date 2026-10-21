"""Parsing rules for telephone intake and e-mail sender names.

Revision 037 made two intake paths work that previously threw their input
away: a phone call that does not quote a ticket number, and an e-mail whose
sender has a display name. Both are pure functions, so these need no database.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.services.intake import split_from_header
from app.services.integrations import IntegrationService


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ('"سعاد فؤاد" <soad@acme.test>', ("سعاد فؤاد", "soad@acme.test")),
        ("Karim Nabil <karim@acme.test>", ("Karim Nabil", "karim@acme.test")),
        ("  Mona  Said   <mona@acme.test> ", ("Mona Said", "mona@acme.test")),
        # RFC 2047 encoded words are how Arabic names actually arrive.
        ("=?UTF-8?B?2YXZhtmJINiz2LnZitiv?= <m@acme.test>", ("منى سعيد", "m@acme.test")),
        # No display name at all: the address is still usable.
        ("plain@acme.test", (None, "plain@acme.test")),
        ("<plain@acme.test>", (None, "plain@acme.test")),
        # A display name that merely repeats the address tells us nothing, and
        # parseaddr cannot parse it either — the address must still survive.
        ("soad@acme.test <soad@acme.test>", (None, "soad@acme.test")),
        # Nothing we can reply to.
        ("garbage", (None, "")),
        ("Mail Delivery <>", (None, "")),
        ("", (None, "")),
        (None, (None, "")),
        (12345, (None, "")),
    ],
)
def test_from_header_is_split_into_name_and_address(
    header: Any, expected: tuple[str | None, str]
) -> None:
    assert split_from_header(header) == expected


def test_display_name_is_length_capped() -> None:
    name, address = split_from_header(f"{'ا' * 400} <long@acme.test>")  # noqa: RUF001
    assert address == "long@acme.test"
    assert name is not None
    assert len(name) <= 160


def _normalize(provider: str, document: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return IntegrationService._normalize_webhook(  # type: ignore[attr-defined]
        IntegrationService.__new__(IntegrationService), provider, document
    )


def test_a_call_without_a_ticket_number_opens_a_request() -> None:
    """Somebody ringing a broker is usually starting something, not quoting a number."""
    events = _normalize(
        "mock_voice",
        {
            "event_id": "call-1",
            "provider_call_id": "CA-1",
            "caller": "+201500000001",
            "caller_name": "سعاد فؤاد",
            "transcript": "محتاجة موافقة على أشعة",
            "duration_seconds": 93,
            "consent_confirmed": True,
        },
    )
    assert len(events) == 1
    kind, event_id, payload = events[0]
    assert kind == "inbound_call"
    assert event_id == "call-1"
    assert payload["sender"] == "+201500000001"
    assert payload["sender_name"] == "سعاد فؤاد"
    assert payload["body"] == "محتاجة موافقة على أشعة"
    assert payload["duration_seconds"] == 93
    assert payload["consent_confirmed"] is True


def test_a_call_quoting_a_ticket_still_joins_that_request() -> None:
    events = _normalize(
        "mock_voice",
        {
            "event_id": "call-2",
            "provider_call_id": "CA-2",
            "caller": "+201500000002",
            "transcript": "بخصوص الطلب REQ-2026-000007",
        },
    )
    kind, _, payload = events[0]
    assert kind == "inbound_voice"
    assert payload["ticket_no"] == "REQ-2026-000007"


def test_a_call_with_neither_ticket_nor_caller_is_rejected() -> None:
    with pytest.raises(HTTPException) as caught:
        _normalize("mock_voice", {"event_id": "call-3", "transcript": "..."})
    assert caught.value.status_code == 422


@pytest.mark.parametrize("duration", ["abc", None, -5, 999_999, {"n": 1}])
def test_an_implausible_call_duration_is_dropped_not_stored(duration: Any) -> None:
    _, _, payload = _normalize(
        "mock_voice",
        {
            "event_id": "call-4",
            "caller": "+201500000003",
            "transcript": "تفاصيل",
            "duration_seconds": duration,
        },
    )[0]
    assert payload["duration_seconds"] is None


def test_an_email_without_a_ticket_number_opens_a_request() -> None:
    """The e-mail gate used to require a quoted ticket, so no e-mail could start one."""
    events = _normalize(
        "email_webhook",
        {
            "event_id": "mail-1",
            "from": '"سعاد فؤاد" <soad@acme.test>',
            "subject": "طلب موافقة",
            "text": "محتاجة موافقة على تحاليل",
        },
    )
    assert len(events) == 1
    kind, _, payload = events[0]
    assert kind == "inbound_email"
    assert payload["ticket_no"] is None
    assert payload["sender"] == "soad@acme.test"
    # Without this the request is filed under the placeholder the database
    # generates from a row counter ("عميل 0003").
    assert payload["sender_name"] == "سعاد فؤاد"


def test_an_email_quoting_a_ticket_still_joins_that_request() -> None:
    _, _, payload = _normalize(
        "email_webhook",
        {
            "event_id": "mail-2",
            "from": "karim@acme.test",
            "subject": "Re: REQ-2026-000009",
            "text": "شكرا",
        },
    )[0]
    assert payload["ticket_no"] == "REQ-2026-000009"


def test_an_attachment_only_email_is_accepted() -> None:
    """A photo of a card with no covering text is a complete request here."""
    _, _, payload = _normalize(
        "email_webhook",
        {
            "event_id": "mail-3",
            "from": "mona@acme.test",
            "subject": "",
            "text": "",
            "attachments": [{"filename": "card.jpg", "content_b64": "AAAA"}],
        },
    )[0]
    assert payload["attachments"]


def test_an_email_with_no_usable_sender_is_rejected() -> None:
    with pytest.raises(HTTPException) as caught:
        _normalize("email_webhook", {"event_id": "mail-4", "from": "", "text": "hello"})
    assert caught.value.status_code == 422
