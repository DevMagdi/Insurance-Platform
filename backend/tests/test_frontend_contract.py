"""Contract check: the payloads the SPA really sends must satisfy the API schemas.

The Playwright suite mocks ``/api`` at the network layer, so a frontend that
sends a body the API rejects can never fail there. This module pins the exact
request bodies the SPA builds (see ``frontend/src``) against the real Pydantic
models, so a drift between the two sides fails the backend test suite.

Run with:  ../.venv/bin/python -m pytest tests/test_frontend_contract.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.privacy import ConsentUpdate
from app.domain.schemas import (
    AccessSpec,
    AIAnalysisRequest,
    AIReviewRequest,
    ApiKeyCreate,
    AssignmentRequest,
    AssignmentRuleSpec,
    BulkAction,
    BusinessHoursTestRequest,
    CategorySpec,
    CompanySpec,
    ComplaintFilter,
    CsatRequest,
    CustomerProfileUpdate,
    CustomerVerifyRequest,
    DepartmentAssignment,
    DepartmentSpec,
    EscalationRequest,
    ExportRequest,
    GuestTrackRequest,
    MergeRequest,
    MobileOTPRequest,
    PlatformBrandRequest,
    ReplyRequest,
    ReportFilter,
    ReportScheduleSpec,
    RevealReason,
    RoleClone,
    RoleSpec,
    SavedViewCreate,
    SlaRuleSpec,
    SlaSimulationRequest,
    StaffInviteRequest,
    StaffProfileEdit,
    StatusChangeRequest,
    TeamSpec,
    TemplateSpec,
    TotpPolicyRequest,
    UploadPlan,
    VoiceConsentRequest,
    WhatsAppSendRequest,
)

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
NOW = datetime.now(UTC).isoformat()


def _valid(model: Any, payload: dict[str, Any]) -> None:
    """Assert the payload validates; a failure message names the offending field."""
    model.model_validate(payload)


def _invalid(model: Any, payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)


# --------------------------------------------------------------------------- #
# Admin console: frontend/src/features/admin/OperationsPage.tsx
# --------------------------------------------------------------------------- #
def test_sla_rule_payload_from_operations_page_is_accepted() -> None:
    """The SLA editor posts ``business_hours: {}`` for "continuous coverage".

    The database definer treats an empty object as "no calendar" (see
    ``catalog_payload`` in app/api/catalog.py), so the API must accept it too.
    """
    payload = {
        "company_id": None,
        "insurance_type": None,
        "priority": None,
        "response_minutes": 60,
        "resolution_minutes": 240,
        "business_hours": {},
        "is_active": True,
    }
    _valid(SlaRuleSpec, payload)


def test_sla_rule_payload_without_business_hours_is_accepted() -> None:
    _valid(
        SlaRuleSpec,
        {
            "company_id": None,
            "insurance_type": None,
            "priority": None,
            "response_minutes": 60,
            "resolution_minutes": 240,
            "is_active": True,
        },
    )


def test_assignment_rule_payload_needs_a_target() -> None:
    """``app_security.save_assignment_rule`` refuses a rule with no target.

    The operations console must therefore require a team or an agent before it
    posts; the API keeps rejecting the ambiguous payload with 422.
    """
    payload = {
        "company_id": None,
        "insurance_type": None,
        "category_id": None,
        "priority": None,
        "target_team_id": None,
        "target_agent_id": None,
        "strategy": "least_workload",
        "sort_order": 100,
        "is_active": True,
    }
    _invalid(AssignmentRuleSpec, payload)


def test_assignment_rule_payload_with_targets_is_accepted() -> None:
    payload = {
        "company_id": UUID_A,
        "insurance_type": "health",
        "category_id": UUID_B,
        "priority": "high",
        "target_team_id": UUID_A,
        "target_agent_id": None,
        "strategy": "round_robin",
        "sort_order": 10,
        "is_active": True,
    }
    _valid(AssignmentRuleSpec, payload)


def test_company_payload_from_operations_page_is_accepted() -> None:
    payload = {
        "slug": "nile-assurance",
        "name_en": "Nile Assurance",
        "name_ar": "النيل للتأمين",
        "contact_emails": ["claims@example.com"],
        "supported_types": ["health", "general"],
        "is_active": True,
    }
    _valid(CompanySpec, payload)


def test_category_payload_from_operations_page_is_accepted() -> None:
    payload = {
        "company_id": UUID_A,
        "parent_id": None,
        "name_en": "Motor Claims",
        "name_ar": "مطالبات السيارات",
        "is_active": True,
    }
    _valid(CategorySpec, payload)


def test_template_payload_from_operations_page_is_accepted() -> None:
    payload = {
        "company_id": None,
        "title": "Welcome reply",
        "body_en": "Hello customer",
        "body_ar": "أهلاً بك",
        "is_active": True,
    }
    _valid(TemplateSpec, payload)


def test_team_payload_from_operations_page_is_accepted() -> None:
    payload = {"name": "Claims", "lead_staff_id": None, "member_ids": []}
    _valid(TeamSpec, payload)


def test_report_schedule_payload_from_operations_page_is_accepted() -> None:
    payload = {
        "name": "Weekly summary",
        "filters": {"window_days": 7, "company_id": None, "insurance_type": None},
        "cron_expression": "0 8 * * 1",
        "time_zone": "Africa/Cairo",
        "recipient_emails": ["team@example.com"],
        "format": "csv",
        "is_active": True,
    }
    _valid(ReportScheduleSpec, payload)


# --------------------------------------------------------------------------- #
# Admin console: frontend/src/features/admin/UsersPage.tsx, BrandSettings.tsx
# --------------------------------------------------------------------------- #
def test_invitation_payload_from_users_page_is_accepted() -> None:
    payload = {
        "email": "new.employee@example.com",
        "full_name": "New Employee",
        "department_id": UUID_A,
    }
    _valid(StaffInviteRequest, payload)


def test_department_payload_from_departments_panel_is_accepted() -> None:
    payload = {"name": "Claims", "description": "Claims team", "is_active": True}
    _valid(DepartmentSpec, payload)


def test_department_assignment_payload_is_accepted() -> None:
    _valid(DepartmentAssignment, {"department_id": UUID_A})


def test_brand_payload_from_brand_settings_is_accepted() -> None:
    _valid(
        PlatformBrandRequest,
        {"platform_name": "Assurance Egypt", "logo_url": "/insurance-logo.svg"},
    )


def test_brand_payload_after_logo_upload_is_accepted() -> None:
    """The logo endpoint answers with ``/api/public/brand/logo``."""
    _valid(
        PlatformBrandRequest,
        {"platform_name": "Assurance Egypt", "logo_url": "/api/public/brand/logo"},
    )


def test_role_payload_from_roles_page_is_accepted() -> None:
    payload = {
        "name": "Claims agent",
        "description": "Handles claims",
        "permissions": ["complaints.view", "complaints.reply_public"],
    }
    _valid(RoleSpec, payload)


def test_role_clone_payload_is_accepted() -> None:
    _valid(RoleClone, {"name": "Claims agent copy"})


def test_staff_profile_edit_payload_is_accepted() -> None:
    _valid(StaffProfileEdit, {"email": "agent@example.com", "full_name": "Agent"})


def test_totp_policy_payload_is_accepted() -> None:
    _valid(TotpPolicyRequest, {"required": False})


# --------------------------------------------------------------------------- #
# Staff workspace: frontend/src/features/complaints/*, frontend/src/pages/*
# --------------------------------------------------------------------------- #
def test_assignment_payload_from_ticket_workspace_is_accepted() -> None:
    _valid(AssignmentRequest, {"agent_id": UUID_A, "team_id": None})


def test_status_payload_from_ticket_workspace_is_accepted() -> None:
    for status in ("assigned", "in_review", "resolved", "transferred"):
        _valid(StatusChangeRequest, {"status": status})


def test_status_payload_rejects_unsupported_transitions() -> None:
    for status in ("new", "reopened"):
        _invalid(StatusChangeRequest, {"status": status})


def test_reply_payload_is_accepted() -> None:
    _valid(ReplyRequest, {"body": "We are investigating."})


def test_escalation_payload_is_accepted() -> None:
    _valid(EscalationRequest, {"reason": "Customer escalated to the regulator"})


def test_reveal_reason_payload_is_accepted() -> None:
    _valid(RevealReason, {"reason": "Case verification for the customer"})


def test_merge_payload_is_accepted() -> None:
    _valid(MergeRequest, {"target_id": UUID_B})


def test_saved_view_payload_from_dashboard_is_accepted() -> None:
    payload = {
        "name": "Urgent cases",
        "criteria": {
            "page": 1,
            "page_size": 20,
            "sort_by": "created_at",
            "sort_dir": "desc",
        },
    }
    _valid(SavedViewCreate, payload)


def test_saved_view_payload_with_dates_is_accepted() -> None:
    payload = {
        "name": "Last 7 days",
        "criteria": {
            "date_from": (datetime.now(UTC) - timedelta(days=7)).isoformat(),
            "date_to": datetime.now(UTC).isoformat(),
            "page": 1,
            "page_size": 20,
        },
    }
    _valid(SavedViewCreate, payload)


def test_bulk_assign_payload_from_dashboard_is_accepted() -> None:
    payload = {
        "action": "assign",
        "complaint_ids": [UUID_A, UUID_B],
        "assignment": {"agent_id": UUID_A, "team_id": None},
        "status": None,
    }
    _valid(BulkAction, payload)


def test_bulk_status_payload_from_dashboard_is_accepted() -> None:
    payload = {
        "action": "status",
        "complaint_ids": [UUID_A],
        "assignment": None,
        "status": "assigned",
    }
    _valid(BulkAction, payload)


def test_export_payload_from_exports_page_is_accepted() -> None:
    end = datetime.now(UTC)
    start = end - timedelta(days=29)
    payload = {
        "kind": "complaints",
        "format": "csv",
        "filters": {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "page": 1,
            "page_size": 100,
        },
    }
    _valid(ExportRequest, payload)


def test_access_spec_payload_from_access_editor_is_accepted() -> None:
    payload = {
        "role_ids": [UUID_A],
        "allow": [],
        "deny": ["users.edit"],
        "all_companies": False,
        "company_ids": [UUID_B],
        "insurance_types": [],
        "ownership": "all",
        "team_ids": [],
    }
    _valid(AccessSpec, payload)


def test_api_key_payload_is_accepted() -> None:
    payload = {
        "permissions": ["complaints.view"],
        "all_companies": True,
        "company_ids": [],
        "expires_at": None,
    }
    _valid(ApiKeyCreate, payload)


# --------------------------------------------------------------------------- #
# Customer portal: frontend/src/lib/api.ts, frontend/src/pages/*
# --------------------------------------------------------------------------- #
def test_customer_otp_start_payload_is_accepted() -> None:
    _valid(MobileOTPRequest, {"mobile": "+201001234567", "email": "customer@example.com"})


def test_customer_otp_verify_payload_is_accepted() -> None:
    payload = {
        "mobile": "+201001234567",
        "code": "123456",
        "national_id": "29006220101234",
        "full_name": "Customer",
        "email": "customer@example.com",
        "whatsapp_available": True,
    }
    _valid(CustomerVerifyRequest, payload)


def test_customer_profile_update_payload_is_accepted() -> None:
    payload = {
        "full_name": "Customer",
        "email": "customer@example.com",
        "whatsapp_available": True,
        "notification_preferences": {"email": True, "whatsapp": False, "sms": False},
    }
    _valid(CustomerProfileUpdate, payload)


def test_customer_profile_update_without_email_is_accepted() -> None:
    payload = {
        "full_name": "Customer",
        "email": None,
        "whatsapp_available": False,
        "notification_preferences": {"email": True, "whatsapp": True, "sms": True},
    }
    _valid(CustomerProfileUpdate, payload)


def test_csat_payload_is_accepted() -> None:
    _valid(CsatRequest, {"score": 5, "comment": "great"})
    _valid(CsatRequest, {"score": 1, "comment": None})


def test_consent_payload_is_accepted() -> None:
    _valid(ConsentUpdate, {"purpose": "complaint_processing", "granted": True})


def test_guest_track_payload_is_accepted() -> None:
    _valid(GuestTrackRequest, {"ticket_no": "CMP-2026-000123", "national_id": "29006220101234"})


def test_upload_plan_payload_is_accepted() -> None:
    payload = {
        "files": [
            {
                "filename": "proof.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 2048,
                "checksum_sha256": "a" * 64,
            }
        ]
    }
    _valid(UploadPlan, payload)


def test_voice_consent_payload_is_accepted() -> None:
    _valid(VoiceConsentRequest, {"enabled": True})


def test_whatsapp_payload_is_accepted() -> None:
    payload = {
        "body": "We are on it",
        "template_name": "claim_update",
        "language": "en",
        "parameters": [],
    }
    _valid(WhatsAppSendRequest, payload)


def test_ai_payloads_are_accepted() -> None:
    _valid(AIAnalysisRequest, {"locale": "en"})
    _valid(AIReviewRequest, {"accept": True})


def test_sla_simulation_payload_is_accepted() -> None:
    payload = {
        "company_id": None,
        "insurance_type": "health",
        "priority": "normal",
        "created_at": NOW,
    }
    _valid(SlaSimulationRequest, payload)


def test_business_hours_preview_payload_is_accepted() -> None:
    payload = {
        "business_hours": {
            "time_zone": "Africa/Cairo",
            "weekly": [{"weekday": 0, "start": "09:00", "end": "17:00"}],
            "holidays": [],
        },
        "minutes": 120,
        "from_at": NOW,
        "company_id": None,
        "insurance_type": None,
    }
    _valid(BusinessHoursTestRequest, payload)


def test_complaint_filter_from_the_staff_table_is_accepted() -> None:
    payload = {
        "status": "in_review",
        "company_id": UUID_A,
        "insurance_type": "health",
        "assigned_agent_id": UUID_B,
        "priority": "high",
        "category_id": None,
        "channel": "portal",
        "date_from": (datetime.now(UTC) - timedelta(days=7)).isoformat(),
        "date_to": datetime.now(UTC).isoformat(),
        "search": "delay",
        "sort_by": "created_at",
        "sort_dir": "desc",
        "page": 1,
        "page_size": 20,
    }
    _valid(ComplaintFilter, payload)


def test_complaint_filter_with_empty_search_is_accepted() -> None:
    """The queue sends an empty search box; the API must not reject it."""
    _valid(ComplaintFilter, {"search": "", "page": 1, "page_size": 20})


# --------------------------------------------------------------------------- #
# The contract itself must stay strict: unknown fields are still rejected.
# --------------------------------------------------------------------------- #
def test_unknown_fields_are_still_rejected() -> None:
    _invalid(DepartmentSpec, {"name": "Claims", "colour": "red"})
    _invalid(ReplyRequest, {"body": "hi", "extra": 1})


def test_generated_uuid_is_stable() -> None:
    """Guard the fixture UUIDs used above (a typo would mask a real mismatch)."""
    assert uuid.UUID(UUID_A).version == 4
    assert uuid.UUID(UUID_B).version == 4


# --------------------------------------------------------------------------- #
# Regressions: the operations console must not build a payload the API
# (or the database) rejects.
# --------------------------------------------------------------------------- #
def test_report_schedule_payload_is_serialized_without_pagination() -> None:
    """A scheduled report has no page; the database contract rejects one.

    ``save_report_schedule`` only accepts the relative filter keys, so the
    pagination and sorting fields inherited from ``ComplaintFilter`` must not
    reach it.
    """
    spec = ReportScheduleSpec.model_validate(
        {
            "name": "Weekly summary",
            "filters": {"window_days": 7, "company_id": None, "insurance_type": None},
            "cron_expression": "0 8 * * 1",
            "time_zone": "Africa/Cairo",
            "recipient_emails": ["team@example.com"],
            "format": "csv",
            "is_active": True,
        }
    )
    payload = spec.model_dump(mode="json", exclude_none=True)
    filters = payload["filters"]
    assert "page" not in filters
    assert "page_size" not in filters
    assert "sort_by" not in filters
    assert "sort_dir" not in filters
    assert filters["window_days"] == 7


def test_report_filter_rejects_pagination_and_sorting() -> None:
    _invalid(ReportFilter, {"window_days": 7, "page": 2})
    _invalid(ReportFilter, {"window_days": 7, "page_size": 50})
    _invalid(ReportFilter, {"window_days": 7, "sort_by": "priority"})
    _invalid(ReportFilter, {"window_days": 7, "sort_dir": "asc"})


def test_report_filter_rejects_absolute_dates() -> None:
    _invalid(
        ReportFilter,
        {"window_days": 7, "date_from": datetime.now(UTC).isoformat()},
    )


def test_team_payload_keeps_the_lead_inside_the_members() -> None:
    """The operations console adds the leader to the member list."""
    _valid(
        TeamSpec,
        {
            "name": "Claims",
            "lead_staff_id": UUID_A,
            "member_ids": [UUID_A, UUID_B],
        },
    )
    _invalid(
        TeamSpec,
        {"name": "Claims", "lead_staff_id": UUID_A, "member_ids": [UUID_B]},
    )


def test_sla_rule_payload_rejects_a_shorter_resolution() -> None:
    _invalid(
        SlaRuleSpec,
        {
            "company_id": None,
            "insurance_type": "health",
            "priority": "normal",
            "response_minutes": 120,
            "resolution_minutes": 60,
            "business_hours": None,
            "is_active": True,
        },
    )


def test_company_payload_rejects_an_invalid_contact_email() -> None:
    _invalid(
        CompanySpec,
        {
            "slug": "nile-assurance",
            "name_en": "Nile Assurance",
            "name_ar": "تأمين النيل",
            "contact_emails": ["not-an-address"],
            "supported_types": ["health"],
            "is_active": True,
        },
    )


def test_report_schedule_payload_rejects_duplicate_recipients() -> None:
    _invalid(
        ReportScheduleSpec,
        {
            "name": "Weekly summary",
            "filters": {"window_days": 7},
            "cron_expression": "0 8 * * 1",
            "time_zone": "Africa/Cairo",
            "recipient_emails": ["team@example.com", "team@example.com"],
            "format": "csv",
            "is_active": True,
        },
    )


def test_report_schedule_payload_rejects_an_invalid_recipient() -> None:
    _invalid(
        ReportScheduleSpec,
        {
            "name": "Weekly summary",
            "filters": {"window_days": 7},
            "cron_expression": "0 8 * * 1",
            "time_zone": "Africa/Cairo",
            "recipient_emails": ["team-at-example"],
            "format": "csv",
            "is_active": True,
        },
    )


def test_access_spec_rejects_an_empty_company_list() -> None:
    """``all_companies: false`` with no company is rejected by the API."""
    _invalid(
        AccessSpec,
        {
            "role_ids": [],
            "allow": [],
            "deny": [],
            "all_companies": False,
            "company_ids": [],
            "insurance_types": [],
            "ownership": "all",
            "team_ids": [],
        },
    )


def test_complaint_filter_accepts_a_single_character_search() -> None:
    """Typing one character in the search box must not break the table."""
    _valid(ComplaintFilter, {"search": "a"})
    _valid(ComplaintFilter, {"search": "ب"})
