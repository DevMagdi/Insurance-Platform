"""Versioned resource.action catalog; roles grant keys, not hard-coded role names."""

from __future__ import annotations

from typing import Final

PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        "complaints.view",
        "complaints.create",
        "complaints.edit",
        "complaints.assign",
        "complaints.reassign",
        "complaints.change_status",
        "complaints.reply_public",
        "complaints.add_internal_note",
        "complaints.merge",
        "complaints.escalate",
        "complaints.delete",
        "complaints.export",
        "complaints.view_attachments",
        "complaints.view_full_national_id",
        "analytics.view",
        "analytics.export",
        "users.view",
        "users.create",
        "users.edit",
        "users.deactivate",
        "users.manage_access",
        "roles.manage",
        "teams.manage",
        "companies.manage",
        "categories.manage",
        "sla.manage",
        "templates.manage",
        "integrations.manage",
        "integrations.send_email",
        "integrations.send_whatsapp",
        "integrations.send_sms",
        "integrations.ai_call",
        "reports.manage",
        "audit.view",
        "settings.manage",
        "storage.manage",
        "requests.intake",
        "corporate.view",
        "corporate.manage",
        # Phase 2: Sales CRM, policy lifecycle, issuing, imports, payments.
        "crm.view",
        "crm.manage",
        "crm.convert",
        "policies.view",
        "policies.manage",
        "issuing.view",
        "issuing.manage",
        "imports.manage",
        "payments.manage",
    }
)


def permission_parts(code: str) -> tuple[str, str]:
    """Reject unknown or malformed permissions before storing them in roles."""
    if code not in PERMISSIONS:
        raise ValueError(f"Unknown permission: {code}")
    resource, action = code.split(".", maxsplit=1)
    return resource, action
