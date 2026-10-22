"""Parsing rules for inbound requests (WhatsApp, e-mail, SMS, phone).

These cover the message shapes the brokerage actually receives: Arabic free
text, a card or policy number written half a dozen different ways, and a
self-introduction that must not swallow the rest of the sentence. They need no
database, so they run in any environment.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.services.intake import (
    InboundEvent,
    IntakeError,
    RequestIntakeService,
    _error_code,
    derive_subject,
    extract_card_number,
    extract_mobile,
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


class _Result:
    """The two shapes of result the intake service asks SQLAlchemy for."""

    def __init__(self, row: Any = None) -> None:
        self._row = row

    def one_or_none(self) -> Any:
        return self._row

    def one(self) -> Any:
        assert self._row is not None, "the test did not configure a row"
        return self._row


class _FakeSession:
    """Records the SQL and the bound parameters instead of reaching Postgres.

    The defects these tests cover are all *parameter* defects: the value sent
    for a placeholder had the wrong Python type, or came from the wrong field.
    A real connection is not needed to see that, which matters because the
    suites that do use one are excluded from the default run.
    """

    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows or {}

    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(statement).split())
        self.calls.append((sql, params or {}))
        for marker, row in self._rows.items():
            if marker in sql:
                return _Result(row)
        return _Result(None)

    def params_for(self, marker: str) -> dict[str, Any]:
        for sql, params in self.calls:
            if marker in sql:
                return params
        raise AssertionError(f"no statement containing {marker!r} was executed")


def _service() -> RequestIntakeService:
    return RequestIntakeService(blobs=None)


def _email_event(body: str = "", sender: str = "soad@acme.test") -> InboundEvent:
    return InboundEvent(
        channel="email",
        external_event_id="evt-1",
        sender=sender,
        body=body,
        subject="تأخر صرف تعويض",
        company_id=uuid4(),
    )


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (("+201004594694",), "+201004594694"),
        (("01004594694",), "01004594694"),
        (("soad@acme.test", "اتصل على 01004594694 لو سمحت"), "01004594694"),
        (("soad@acme.test",), None),
        (("مفيش رقم معايا",), None),
        # A card number is not a mobile, even though both are digits.
        (("الكارت رقم 99881234",), None),
    ],
)
def test_mobile_is_recognised_without_matching_an_address(
    values: tuple[str, ...], expected: str | None
) -> None:
    assert extract_mobile(*values) == expected


async def test_continuation_window_is_bound_as_an_interval_not_a_string() -> None:
    """PostgreSQL types a bare parameter inside CAST(... AS interval).

    asyncpg then requires a timedelta and rejects a string client-side, which
    failed intake on every channel before a request could be created. The
    error surfaced only as "sqlstate_22000", so pin the bound type here.
    """
    session = _FakeSession()
    await _service()._resolve_target(session, _email_event(), "نص الرسالة")

    sql, params = session.calls[-1]
    assert "CAST(:window AS interval)" in sql
    assert isinstance(params["window"], timedelta)


async def test_email_request_is_created_with_a_mobile_from_the_message() -> None:
    """The sender of an e-mail is an address; the request needs a number."""
    row = SimpleNamespace(
        complaint_id=uuid4(), complaint_created_at=datetime.now(UTC), ticket_no="REQ-2026-000009"
    )
    session = _FakeSession({"create_channel_request": row})
    event = _email_event(body="قدمت مطالبة من شهر. رقمي 01004594694")

    await _service()._create_request(session, event, event.body, uuid4())

    params = session.params_for("create_channel_request")
    assert params["mobile"] == "01004594694"
    assert params["mobile"] != event.sender


async def test_email_without_any_mobile_is_refused_with_a_readable_code() -> None:
    session = _FakeSession()
    event = _email_event(body="لسه مفيش رد على المطالبة")

    with pytest.raises(IntakeError) as excinfo:
        await _service()._create_request(session, event, event.body, uuid4())

    assert excinfo.value.code == "sender_mobile_missing"


async def test_caller_number_is_used_directly_for_a_telephone_request() -> None:
    """Phone and WhatsApp senders are already numbers; nothing should change."""
    row = SimpleNamespace(
        complaint_id=uuid4(), complaint_created_at=datetime.now(UTC), ticket_no="REQ-2026-000010"
    )
    session = _FakeSession({"create_channel_request": row})
    event = InboundEvent(
        channel="phone",
        external_event_id="call-1",
        sender="+201004594694",
        body="اتصلت بخصوص تأخر صرف التعويض",
        company_id=uuid4(),
    )

    await _service()._create_request(session, event, event.body, uuid4())

    assert session.params_for("create_channel_request")["mobile"] == "+201004594694"


def test_database_rejection_reports_its_reason_not_a_bare_sqlstate() -> None:
    """asyncpg carries ``.message``; reading only psycopg's ``.diag`` hid it."""
    asyncpg_error = SimpleNamespace(message="intake mobile invalid", sqlstate="22023")
    wrapped = RuntimeError("statement failed")
    wrapped.orig = asyncpg_error  # type: ignore[attr-defined]

    assert _error_code(wrapped) == "intake mobile invalid"


def test_sqlstate_is_still_reported_when_no_message_is_available() -> None:
    bare = RuntimeError("statement failed")
    bare.orig = SimpleNamespace(sqlstate="22023")  # type: ignore[attr-defined]

    assert _error_code(bare) == "sqlstate_22023"
