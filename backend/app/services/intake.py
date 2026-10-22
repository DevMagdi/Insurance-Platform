"""Turn an inbound channel event into a real request.

Before this module existed an inbound WhatsApp message, e-mail or SMS could
only *append* to a ticket whose number the sender had quoted verbatim
(``CMP-2026-000123``). Anything else was dropped: the WhatsApp handler did
``continue`` and the mailbox poller marked the message seen. For a brokerage
whose customers simply send "محتاج موافقة على أشعة" with a photo of their
card, that meant losing essentially all of its intake.

The rules implemented here are deliberately simple and auditable:

* A quoted ticket number still wins — the message joins that request.
* Otherwise the sender's mobile number identifies them. A known mobile with a
  recent open request continues that conversation; anything else opens a new
  request.
* A request needs only a policy number or a card number. Both are extracted
  from the message text when present, and staff can fill the gap later from
  the intake inbox rather than the message being rejected.
* Nothing is ever silently discarded. Every event lands in
  ``request_intake_events`` with its outcome, so "we never received it" can be
  answered from the database.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parseaddr
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class IntakeError(RuntimeError):
    """An inbound message that intake refuses, with a stable machine code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _error_code(exc: BaseException) -> str:
    """A short, stable code for the intake inbox instead of a stack trace."""
    if isinstance(exc, IntakeError):
        return exc.code[:120]
    # A database rejection carries the real reason; SQLAlchemy's own ``.code``
    # is only a documentation shortlink ("f405") and must not win here.
    original = getattr(exc, "orig", None)
    # asyncpg exposes ``.message``; psycopg puts the same text behind
    # ``.diag.message_primary``. Reading only the psycopg attribute meant every
    # database rejection degraded to a bare "sqlstate_22023", which hides the
    # reason an operator needs and made real defects very hard to find.
    message = getattr(original, "message", None) or getattr(
        getattr(original, "diag", None), "message_primary", None
    )
    if isinstance(message, str) and message:
        return message.strip().splitlines()[0][:120]
    sqlstate = getattr(original, "sqlstate", None)
    if isinstance(sqlstate, str) and sqlstate:
        return f"sqlstate_{sqlstate}"
    return type(exc).__name__[:120]


TICKET_RE = re.compile(r"\b(?:REQ|CMP)-\d{4}-\d{6,}\b", re.IGNORECASE)

# Egyptian mobiles, with or without the country code: 01x……… / +201x……… .
# Shared with ``minimize_pii`` so the pattern that redacts a number and the
# pattern that recognises one can never drift apart.
EGYPT_MOBILE_RE = re.compile(r"(?<!\w)(?:\+?20|0)?1[0125]\d{8}(?!\d)")

# An optional "رقم" / "no." may sit between the keyword and the value, which
# is how people actually write it: "رقم الكارت: 12345", "الكارت رقم 998877",
# "card no A-55", "وثيقة رقم POL-5541".
_REF_GAP = r"(?:\s*(?:رقم|no\.?|number|#|:|\.|-))*\s*"
CARD_RE = re.compile(
    r"(?:card|كارت|الكارت|بطاق[ةه]|البطاق[ةه])" + _REF_GAP + r"([A-Za-z0-9][A-Za-z0-9/_-]{2,40})",
    re.IGNORECASE,
)
POLICY_RE = re.compile(
    r"(?:policy|وثيق[ةه]|الوثيق[ةه]|بوليص[ةه]|البوليص[ةه])"
    + _REF_GAP
    + r"([A-Za-z0-9][A-Za-z0-9/_-]{3,40})",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

# A self-introduction ends at the first connective or verb, otherwise half the
# sentence ends up in the customer's name.
_NAME_STOPS = tuple(
    [
        "محتاج",
        "عايز",
        "عاوز",
        "عايزة",
        "ارجو",
        "أرجو",
        "برجاء",
        "رجاء",
        "بخصوص",
        "عندي",
        "معايا",
        "معي",
        "ابعت",
        "ابغى",
        "اريد",
        "أريد",
        "لو",
        "من",
        "في",
        "على",
        "الى",
        "إلى",
        "وكنت",
        "كنت",
        "بس",
        "كمان",
        "ممكن",
        "حابب",
        "حابة",
        "please",
        "need",
        "want",
        "kindly",
        "regarding",
        "about",
        "the",
        "and",
        "for",
        "with",
    ]
)
_EMAIL_IN_HEADER_RE = re.compile(r"[^\s<>,;\"]+@[^\s<>,;\"]+\.[A-Za-z]{2,}")
_NAME_RE = re.compile(
    r"(?:اسمي|إسمي|انا|أنا|معاك|معك|my name is|i am|i'm|this is)\s+((?:[^\W\d_]+[ \t]*){1,5})",
    re.IGNORECASE | re.UNICODE,
)


def extract_ticket_no(*values: str) -> str | None:
    for value in values:
        match = TICKET_RE.search(value or "")
        if match:
            return match.group(0).upper()
    return None


def extract_card_number(text_value: str) -> str | None:
    match = CARD_RE.search(text_value or "")
    return match.group(1).strip() if match else None


def extract_policy_number(text_value: str) -> str | None:
    match = POLICY_RE.search(text_value or "")
    return match.group(1).strip() if match else None


def extract_mobile(*values: str) -> str | None:
    """The mobile that identifies the sender.

    On WhatsApp, SMS and the telephone the sender *is* a number, so the first
    value matches. E-mail carries an address instead, and the database rejects
    anything ``normalize_eg_mobile`` cannot parse, so the message text is the
    only place a number can come from.
    """
    for value in values:
        match = EGYPT_MOBILE_RE.search(value or "")
        if match:
            return match.group(0)
    return None


def _is_stop_word(word: str) -> bool:
    """True when a token starts a new clause rather than continuing a name."""
    folded = word.casefold()
    for stop in _NAME_STOPS:
        if folded == stop:
            return True
        # Prefix matching catches gender/tense endings ("محتاج" -> "محتاجة"),
        # but only on long stems: short function words like "من" are also the
        # start of real names such as "منى".
        if len(stop) >= 4 and folded.startswith(stop) and len(folded) - len(stop) <= 2:
            return True
    return False


def extract_name(text_value: str, fallback: str | None = None) -> str | None:
    """Best-effort sender name from the message body, else the profile name."""
    match = _NAME_RE.search(text_value or "")
    if match:
        words: list[str] = []
        for word in _WORD_RE.findall(match.group(1)):
            # "و" prefixes a word to mean "and", so strip it before matching.
            stem = word[1:] if word.startswith("و") and len(word) > 3 else word
            if _is_stop_word(word) or _is_stop_word(stem):
                break
            words.append(word)
            if len(words) == 3:
                break
        candidate = " ".join(words)[:160].strip()
        if len(candidate) >= 2:
            return candidate
    if fallback:
        cleaned = fallback.strip()[:160]
        if len(cleaned) >= 2:
            return cleaned
    return None


def split_from_header(value: Any) -> tuple[str | None, str]:
    """Split an e-mail ``From`` header into its display name and address.

    ``"سعاد فؤاد" <soad@acme.test>`` carries the sender's real name, which is
    very often the only name the brokerage will ever learn for an e-mail
    request — without it the request is filed under the placeholder the
    database generates from the row counter.

    Returns ``(None, "")`` for anything that is not a usable address, which
    the caller treats as "not an inbound message".
    """
    raw = value if isinstance(value, str) else ""
    name, address = parseaddr(raw.strip())
    if "@" not in address:
        # parseaddr gives up on headers like `a@b.test <a@b.test>`, where the
        # display name is itself an unquoted address. Senders really do that,
        # and dropping the mail over it would lose a customer's request.
        fallback = _EMAIL_IN_HEADER_RE.search(raw)
        if fallback is None:
            return None, ""
        name, address = "", fallback.group(0)
    decoded: list[str] = []
    for chunk, charset in decode_header(name):
        if isinstance(chunk, bytes):
            try:
                decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                decoded.append(chunk.decode("utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    display = " ".join("".join(decoded).split())[:160].strip("\"' ")
    # A display name that is just the address again tells us nothing.
    if display.casefold() == address.casefold():
        display = ""
    return (display or None), address[:254]


def derive_subject(body: str, intake_source: str) -> str:
    """A short, human subject line from a free-text message."""
    cleaned = " ".join((body or "").split())
    if not cleaned:
        return {
            "whatsapp": "طلب وارد من واتساب",
            "email": "طلب وارد من البريد الإلكتروني",
            "phone": "طلب وارد من مكالمة هاتفية",
            "sms": "طلب وارد من رسالة نصية",
        }.get(intake_source, "طلب وارد")
    if len(cleaned) <= 80:
        return cleaned
    cut = cleaned[:80].rsplit(" ", 1)[0]
    return f"{cut}…"


@dataclass
class InboundMedia:
    """One attachment that arrived with an inbound message."""

    media_id: str
    mime_type: str = "application/octet-stream"
    filename: str = "attachment.bin"
    caption: str | None = None
    content: bytes | None = None


@dataclass
class InboundEvent:
    """A normalised inbound message from any channel."""

    channel: str
    external_event_id: str
    sender: str
    body: str = ""
    sender_name: str | None = None
    subject: str | None = None
    company_id: UUID | None = None
    media: list[InboundMedia] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def intake_source(self) -> str:
        return self.channel


@dataclass
class IntakeOutcome:
    state: str
    complaint_id: UUID | None = None
    complaint_created_at: datetime | None = None
    ticket_no: str | None = None
    message_id: UUID | None = None
    attachments: int = 0
    error_code: str | None = None
    duplicate: bool = False


class RequestIntakeService:
    """Creates and continues requests from inbound channel events.

    All persistence goes through SECURITY DEFINER helpers so this works from
    the restricted ``app_worker`` login, which has no table privileges at all.
    """

    # A follow-up message within this window continues the conversation
    # instead of opening a duplicate request.
    CONTINUATION_WINDOW = timedelta(hours=72)
    MAX_MEDIA_BYTES = 10 * 1024 * 1024

    def __init__(self, blobs: Any, default_company_id: UUID | None = None) -> None:
        self.blobs = blobs
        self.default_company_id = default_company_id

    # ------------------------------------------------------------------ main

    async def ingest(
        self, db: AsyncSession, event: InboundEvent, *, company_id: UUID | None = None
    ) -> IntakeOutcome:
        target_company = company_id or event.company_id or self.default_company_id
        body = event.body or ""
        captions = " ".join(m.caption for m in event.media if m.caption)
        searchable = f"{event.subject or ''}\n{body}\n{captions}"

        settled = await self._already_settled(db, event)
        if settled is not None:
            return settled

        # The savepoint matters: if any step below fails, the surrounding
        # transaction is still usable afterwards, so the audit row recording
        # the failure can actually be written.
        savepoint = await db.begin_nested()
        try:
            match = await self._resolve_target(db, event, searchable)
            if match is None:
                if target_company is None:
                    raise IntakeError("no_company_configured")
                match = await self._create_request(db, event, searchable, target_company)
                state = "created"
            else:
                state = "linked"

            message_id = await self._append_message(db, event, match, body or captions)
            stored = await self._store_media(db, event, match, message_id)
            outcome = IntakeOutcome(
                state=state,
                complaint_id=match[0],
                complaint_created_at=match[1],
                ticket_no=match[2],
                message_id=message_id,
                attachments=stored,
            )
            await savepoint.commit()
        except Exception as exc:
            await savepoint.rollback()
            return await self._record(
                db, event, IntakeOutcome("failed", error_code=_error_code(exc)), target_company
            )
        return await self._record(db, event, outcome, target_company)

    # ------------------------------------------------------------- resolution

    async def _already_settled(self, db: AsyncSession, event: InboundEvent) -> IntakeOutcome | None:
        """Short-circuit a webhook Meta has already delivered successfully.

        Only settled events count: a previous failure must still be retried.
        """
        row = (
            await db.execute(
                text(
                    "SELECT state, complaint_id, complaint_created_at"
                    " FROM app_security.intake_event_outcome("
                    " CAST(:channel AS public.communication_channel), :external)"
                ),
                {"channel": event.channel, "external": event.external_event_id},
            )
        ).one_or_none()
        if row is None or row.state is None:
            return None
        return IntakeOutcome(
            state=str(row.state),
            complaint_id=row.complaint_id,
            complaint_created_at=row.complaint_created_at,
            ticket_no=None,
            duplicate=True,
        )

    async def _resolve_target(
        self, db: AsyncSession, event: InboundEvent, searchable: str
    ) -> tuple[UUID, datetime, str] | None:
        """An explicit ticket number, else a recent request from this mobile.

        Routed through a SECURITY DEFINER helper because the worker login
        holds no table privileges on complaints/customers.
        """
        row = (
            await db.execute(
                text(
                    "SELECT complaint_id, complaint_created_at, ticket_no"
                    " FROM app_security.find_intake_target(:ticket, :sender,"
                    " CAST(:window AS interval))"
                ),
                {
                    "ticket": extract_ticket_no(searchable),
                    "sender": event.sender,
                    # PostgreSQL resolves a bare parameter inside CAST(... AS
                    # interval) to the interval type itself, so asyncpg demands
                    # a timedelta and rejects a string client-side. That broke
                    # intake on *every* channel before any request was created.
                    "window": self.CONTINUATION_WINDOW,
                },
            )
        ).one_or_none()
        if row is None or row.complaint_id is None:
            return None
        return (row.complaint_id, row.complaint_created_at, row.ticket_no)

    async def _create_request(
        self, db: AsyncSession, event: InboundEvent, searchable: str, company_id: UUID
    ) -> tuple[UUID, datetime, str]:
        card = extract_card_number(searchable)
        policy = extract_policy_number(searchable)
        if not card and not policy:
            # Neither reference was quoted. The request is still created with a
            # placeholder so the customer is never turned away; the intake
            # inbox flags it for an agent to complete.
            card = "PENDING-REF"
        # The sender is already a number on WhatsApp/SMS/telephone; an e-mail
        # address is not, and the database refuses a request it cannot tie to
        # a customer, so fall back to a number quoted in the message itself.
        mobile = extract_mobile(event.sender) or extract_mobile(searchable)
        if mobile is None:
            raise IntakeError("sender_mobile_missing")
        row = (
            await db.execute(
                text(
                    "SELECT * FROM app_security.create_channel_request("
                    " :company, CAST(:channel AS public.communication_channel), :source,"
                    " :mobile, :name, :subject, :description, :policy, :card,"
                    " CAST(:kind AS public.insurance_type), :reference, NULL)"
                ),
                {
                    "company": company_id,
                    "channel": event.channel,
                    "source": event.intake_source,
                    "mobile": mobile,
                    "name": extract_name(searchable, event.sender_name),
                    "subject": event.subject or derive_subject(event.body, event.intake_source),
                    "description": event.body or event.subject or "(رسالة بدون نص)",
                    "policy": policy,
                    "card": card,
                    "kind": "health",
                    "reference": event.external_event_id,
                },
            )
        ).one()
        return (row.complaint_id, row.complaint_created_at, row.ticket_no)

    # --------------------------------------------------------------- children

    async def _append_message(
        self,
        db: AsyncSession,
        event: InboundEvent,
        match: tuple[UUID, datetime, str],
        body: str,
    ) -> UUID | None:
        message_id = await db.scalar(
            text(
                "SELECT app_security.append_intake_message(:id, :created,"
                " CAST(:channel AS public.communication_channel), :body, :sender, :external)"
            ),
            {
                "id": match[0],
                "created": match[1],
                "channel": event.channel,
                "body": body or "(مرفق بدون نص)",
                "sender": event.sender,
                "external": event.external_event_id,
            },
        )
        return UUID(str(message_id)) if message_id else None

    async def _store_media(
        self,
        db: AsyncSession,
        event: InboundEvent,
        match: tuple[UUID, datetime, str],
        message_id: UUID | None,
    ) -> int:
        """Encrypt and persist each downloaded attachment against the request."""
        stored = 0
        for media in event.media:
            if not media.content:
                continue
            if len(media.content) > self.MAX_MEDIA_BYTES:
                continue
            checksum = hashlib.sha256(media.content).hexdigest()
            attachment_id = UUID(
                hashlib.sha256(f"{event.external_event_id}:{media.media_id}".encode()).hexdigest()[
                    :32
                ]
            )
            ciphertext = self.blobs.encrypt(
                media.content, purpose="attachment", resource_id=attachment_id
            )
            await db.execute(
                text(
                    "SELECT app_security.store_intake_attachment(:id, :created, :message,"
                    " :filename, :mime, :size, :checksum, :content)"
                ),
                {
                    "id": match[0],
                    "created": match[1],
                    "message": message_id,
                    "filename": media.filename,
                    "mime": media.mime_type,
                    "size": len(media.content),
                    "checksum": checksum,
                    "content": ciphertext,
                },
            )
            stored += 1
        return stored

    # ----------------------------------------------------------------- record

    async def _record(
        self,
        db: AsyncSession,
        event: InboundEvent,
        outcome: IntakeOutcome,
        company_id: UUID | None = None,
    ) -> IntakeOutcome:
        await db.execute(
            text(
                "SELECT app_security.record_intake_event(:company,"
                " CAST(:channel AS public.communication_channel), :external, :sender, :name,"
                " :body, :media, CAST(:payload AS jsonb),"
                " CAST(:state AS public.intake_state), :complaint, :created, :error)"
            ),
            {
                "company": company_id or event.company_id,
                "channel": event.channel,
                "external": event.external_event_id,
                "sender": event.sender,
                "name": event.sender_name,
                "body": event.body,
                "media": len(event.media),
                "payload": "{}",
                "state": outcome.state,
                "complaint": outcome.complaint_id,
                "created": outcome.complaint_created_at,
                "error": outcome.error_code,
            },
        )
        return outcome
