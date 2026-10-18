"""Small audited Arabic/English text templates for customer notices and insurer forwarding."""

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
        subject = f"تحديث الشكوى {ticket_no}"
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
) -> tuple[str, str]:
    """Create the forward body without exposing customer national IDs or mobile numbers."""
    if locale == "ar":
        mail_subject = f"شكوى تأمين رقم {ticket_no}: {subject[:160]}"
        header = (
            f"يرجى مراجعة الشكوى رقم {ticket_no}.\n\nرقم الوثيقة: {policy_number}\n"
            f"\nالموضوع: {subject}\n\nالتفاصيل:\n{details}"
        )
        if agent_note:
            header += f"\n\nملاحظة من الوسيط:\n{agent_note}"
    else:
        mail_subject = f"Insurance complaint {ticket_no}: {subject[:160]}"
        header = (
            f"Please review complaint {ticket_no}.\n\nPolicy number: {policy_number}\n"
            f"\nSubject: {subject}\n\nDetails:\n{details}"
        )
        if agent_note:
            header += f"\n\nBroker note:\n{agent_note}"
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
