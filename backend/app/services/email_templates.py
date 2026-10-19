"""Audited text templates and a shared, escaped bilingual HTML email layout."""

from __future__ import annotations

from typing import Literal

Locale = Literal["ar", "en"]

_STATUS_EN = {
    "new": "New",
    "assigned": "Assigned",
    "in_review": "In review",
    "resolved": "Resolved",
    "reopened": "Reopened",
    "transferred": "Transferred to the insurance company",
}
_STATUS_AR = {
    "new": "جديدة",
    "assigned": "تم تعيين مسؤول",
    "in_review": "قيد المراجعة",
    "resolved": "تم الحل",
    "reopened": "أعيد فتحها",
    "transferred": "تم تحويلها إلى شركة التأمين",
}


def render_customer_notice(
    kind: str, ticket_no: str, status: str, locale: str = "en"
) -> tuple[str, str]:
    """Return a locale-specific, PII-minimal status notice."""
    if locale not in ("ar", "en"):
        locale = "en"
    if locale == "ar":
        translated = _STATUS_AR.get(status, status)
        subject = (
            f"تم استلام شكواك {ticket_no}" if kind == "created" else f"تحديث الشكوى {ticket_no}"
        )
        body = (
            f"تم تحديث الشكوى رقم {ticket_no}. الحالة الحالية: {translated}.\n"
            "يمكنك متابعة الشكوى من بوابة العملاء."
        )
        return subject, body
    translated = _STATUS_EN.get(status, status)
    subject = f"Complaint update — {ticket_no}"
    body = (
        f"Your complaint {ticket_no} has been updated. Current status: {translated}.\n"
        "You can follow the complaint from the customer portal."
    )
    if kind == "created":
        subject = f"We received your complaint — {ticket_no}"
    return subject, body


def render_customer_reply(ticket_no: str, body: str, locale: str = "en") -> tuple[str, str]:
    """Wrap an agent-authored response in a localized, ticket-numbered notice."""
    safe_body = body.strip()[:5000]
    if locale == "ar":
        return (
            f"رد جديد على الشكوى {ticket_no}",
            f"ردنا على الشكوى رقم {ticket_no}:\n\n{safe_body}\n\nيمكنك الرد من بوابة العملاء.",
        )
    return (
        f"Response to complaint {ticket_no}",
        (
            f"Our response to complaint {ticket_no}:\n\n{safe_body}\n\n"
            "You can reply from the customer portal."
        ),
    )


def render_insurer_forward(
    *,
    ticket_no: str,
    subject: str,
    details: str,
    policy_number: str,
    locale: str,
    agent_note: str | None = None,
    insurance_type: str = "—",
) -> tuple[str, str]:
    """Create the forward body without exposing customer national IDs or mobile numbers."""
    if locale == "ar":
        mail_subject = f"شكوى تأمين رقم {ticket_no}: {subject[:160]}"
        header = (
            f"يرجى مراجعة الشكوى رقم {ticket_no}.\n\nرقم الوثيقة: {policy_number}\n"
            f"نوع التأمين: {insurance_type}\n\nالموضوع: {subject}\n\nوصف العميل:\n{details}"
        )
        if agent_note:
            header += f"\n\nملاحظة من الوسيط:\n{agent_note}"
    else:
        mail_subject = f"Insurance complaint {ticket_no}: {subject[:160]}"
        header = (
            f"Please review complaint {ticket_no}.\n\nPolicy number: {policy_number}\n"
            f"Insurance type: {insurance_type}\n\nSummary / Subject: {subject}\n\n"
            f"Customer-visible description / Details:\n{details}"
        )
        if agent_note:
            header += f"\n\nAgent note / Broker note:\n{agent_note}"
    return mail_subject, header[:20_000]


def render_staff_invitation(url: str, *, reset: bool = False) -> tuple[str, str]:
    if reset:
        return (
            "Reset your password | إعادة تعيين كلمة المرور",
            "لإعادة تعيين كلمة المرور افتح الرابط الآمن أدناه. لا تشاركه مع أي شخص.\n"
            "Reset your password using the secure one-time link below.\n"
            f"{url}\n\nThis link expires in 48 hours. Ignore it if you did not expect this email.",
        )
    return (
        "Platform invitation | دعوة لإنشاء حسابك",
        "تمت دعوتك للانضمام إلى المنصة. أنشئ كلمة المرور وأكمل بياناتك وصورتك الشخصية.\n"
        "You have been invited to the platform. Create your password and complete "
        "your personal profile and photo using this secure one-time link:\n"
        f"{url}\n\nThis invitation expires in 48 hours. Do not share this link.",
    )


def branded_email(
    title: str,
    body: str,
    *,
    brand: str = "Insurance",
    public_url: str = "",
    support_email: str = "support@example.invalid",
    locale: str = "en",
) -> tuple[str, str]:
    """Shared final rendering boundary: all content is text, never trusted HTML."""
    from html import escape
    from urllib.parse import urlsplit

    rtl = locale == "ar" or any("\u0600" <= c <= "\u06ff" for c in body)
    lang, direction = ("ar", "rtl") if rtl else ("en", "ltr")
    parsed = urlsplit(public_url)
    origin = public_url.rstrip("/") if parsed.scheme in {"http", "https"} else ""
    footer = f"{brand}\nSupport: {support_email}\nThis is an automated message from {brand}."
    if origin:
        footer += f"\nPortal: {origin}"
    text = f"{body.rstrip()}\n\n{footer}"
    logo = (
        f'<img src="{escape(origin, quote=True)}/api/public/brand/logo" '
        f'alt="{escape(brand, quote=True)}" width="160" style="max-width:160px;height:auto">'
        if origin
        else ""
    )
    # Promote standalone HTTP(S) links (including one-time invitations) to safe CTAs.
    paragraphs = []
    for line in body.splitlines():
        value = line.strip()
        if value.startswith(("https://", "http://")) and " " not in value:
            paragraphs.append(
                f'<p><a style="color:#047857" href="{escape(value, quote=True)}">'
                f"{'فتح الرابط الآمن' if rtl else 'Open secure link'}</a></p>"
            )
        else:
            paragraphs.append(f'<p style="margin:0 0 12px">{escape(line) or "&nbsp;"}</p>')
    html = (
        f'<!doctype html><html lang="{lang}" dir="{direction}"><head>'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta charset="utf-8"></head><body style="margin:0;background:#f1f5f9;'
        'font-family:Arial,sans-serif;color:#172b4d">'
        '<table role="presentation" width="100%"><tr><td align="center" style="padding:24px">'
        '<table role="presentation" width="100%" style="max-width:640px;background:white;'
        'border-top:5px solid #b89549"><tr><td style="padding:28px">'
        f'{logo}<div style="font-weight:bold;color:#172b4d">{escape(brand)}</div>'
        f'<h1 style="font-size:24px">{escape(title)}</h1>'
        + "".join(paragraphs)
        + (
            f'<p><a href="{escape(origin, quote=True)}" style="color:#047857">'
            f"{'بوابة المنصة' if rtl else 'Visit the portal'}</a></p>"
            if origin
            else ""
        )
        + '<hr style="border:0;border-top:1px solid #e2e8f0">'
        f'<div style="font-size:12px;color:#475569">{escape(footer).replace(chr(10), "<br>")}'
        "</div></td></tr></table></td></tr></table></body></html>"
    )
    return text, html
