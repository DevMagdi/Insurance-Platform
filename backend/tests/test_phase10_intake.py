"""Parsing rules for inbound requests (WhatsApp, e-mail, SMS, phone).

These cover the message shapes the brokerage actually receives: Arabic free
text, a card or policy number written half a dozen different ways, and a
self-introduction that must not swallow the rest of the sentence. They need no
database, so they run in any environment.
"""

from __future__ import annotations

import pytest

from app.services.intake import (
    InboundEvent,
    derive_subject,
    extract_card_number,
    extract_name,
    extract_policy_number,
    extract_ticket_no,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("السلام عليكم محتاج موافقة اشعة رقم الكارت: 99881234", "99881234"),
        ("الكارت رقم 1234567 محتاج موافقة", "1234567"),
        ("card no A-7781 please approve", "A-7781"),
        ("انا منى سعيد، الكارت 445566", "445566"),
        ("بطاقة 7788/B", "7788/B"),
        ("مفيش رقم معايا", None),
    ],
)
def test_card_number_is_read_from_free_text(message: str, expected: str | None) -> None:
    assert extract_card_number(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("اسمي أحمد ومحتاج موقف وثيقة رقم POL-5541", "POL-5541"),
        ("policy number 889912 مطلوب تجديد", "889912"),
        ("بوليصة 44/A/2026", "44/A/2026"),
        ("عايز اعرف الشبكة الطبية", None),
    ],
)
def test_policy_number_is_read_from_free_text(message: str, expected: str | None) -> None:
    assert extract_policy_number(message) == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("اسمي أحمد محمود ومحتاج أعرف موقف وثيقتي", "أحمد محمود"),
        ("السلام عليكم، اسمي منى سعيد ومحتاجة موافقة", "منى سعيد"),
        ("انا محمد علي حسن", "محمد علي حسن"),
        ("my name is Sara Hassan, card 7781", "Sara Hassan"),
        # "من" (from) must not be mistaken for the start of the name "منى".
        ("انا من القاهرة", None),
        ("اسمي نورا", "نورا"),
    ],
)
def test_name_stops_at_the_end_of_the_introduction(message: str, expected: str | None) -> None:
    assert extract_name(message) == expected


def test_name_falls_back_to_the_whatsapp_profile_name() -> None:
    assert extract_name("محتاج موافقة", "Mona Said") == "Mona Said"
    assert extract_name("محتاج موافقة", None) is None


@pytest.mark.parametrize(
    "message",
    ["بخصوص الطلب REQ-2026-000012 محتاج تحديث", "re: cmp-2026-000012 update"],
)
def test_quoted_ticket_number_is_recognised(message: str) -> None:
    assert extract_ticket_no(message) in {"REQ-2026-000012", "CMP-2026-000012"}


def test_messages_without_a_ticket_number_are_not_rejected() -> None:
    """The old behaviour dropped these outright; intake must still accept them."""
    assert extract_ticket_no("محتاج موافقة على أشعة") is None


def test_subject_is_derived_and_truncated() -> None:
    assert derive_subject("", "whatsapp") == "طلب وارد من واتساب"
    assert derive_subject("", "phone") == "طلب وارد من مكالمة هاتفية"
    short = "محتاج موافقة"
    assert derive_subject(short, "whatsapp") == short
    long_text = "محتاج موافقة على أشعة رنين مغناطيسي " * 10
    derived = derive_subject(long_text, "whatsapp")
    assert len(derived) <= 81
    assert derived.endswith("…")


def test_inbound_event_defaults_are_safe() -> None:
    event = InboundEvent(channel="whatsapp", external_event_id="wamid.1", sender="+201000000000")
    assert event.media == []
    assert event.intake_source == "whatsapp"
    # Separate events must not share the same mutable default.
    other = InboundEvent(channel="email", external_event_id="m2", sender="a@b.example")
    assert event.media is not other.media
