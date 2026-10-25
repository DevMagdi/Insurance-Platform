"""Excel/CSV parsing, column mapping and de-duplication for CRM lead imports.

Pure and database-free (unit-tested offline). ``.xlsx``/``.csv`` reuse the
platform parser (`app.services.crm.parse_tabular`, Arabic header aliases);
legacy ``.xls`` is read with xlrd. File contents are never trusted: every
row is mapped, normalized and validated here, and anything unusable becomes
a per-row error with a stable machine-readable reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.domain.enums import CrmLeadSource
from app.services.crm import _canonical_header, _rows_from_matrix, normalize_mobile, parse_tabular

MAX_IMPORT_BYTES = 5_242_880
_ACCEPTED = (".xlsx", ".xls", ".csv")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Extra header spellings seen in client lead sheets (Arabic + English).
CRM_HEADER_ALIASES: dict[str, str] = {
    "الاسم بالكامل": "full_name",
    "اسم العميل": "full_name",
    "full name": "full_name",
    "customer name": "full_name",
    "رقم الهاتف": "phone",
    "التليفون": "phone",
    "mobile number": "phone",
    "phone number": "phone",
    "الايميل": "email",
    "e-mail": "email",
    "مصدر العميل": "source",
    "lead source": "source",
    "note": "notes",
    "comment": "notes",
    "تعليق": "notes",
    "الحالة": "status",
}

_SOURCE_WORDS: dict[str, str] = {
    "whatsapp": CrmLeadSource.WHATSAPP.value,
    "واتساب": CrmLeadSource.WHATSAPP.value,
    "call": CrmLeadSource.CALL.value,
    "phone": CrmLeadSource.CALL.value,
    "مكالمة": CrmLeadSource.CALL.value,
    "email": CrmLeadSource.EMAIL.value,
    "بريد": CrmLeadSource.EMAIL.value,
    "website": CrmLeadSource.PORTAL.value,
    "portal": CrmLeadSource.PORTAL.value,
    "موقع": CrmLeadSource.PORTAL.value,
    "manual": CrmLeadSource.MANUAL.value,
    "يدوي": CrmLeadSource.MANUAL.value,
}


def validate_upload_name(filename: str) -> str:
    lowered = (filename or "").lower()
    if not lowered.endswith(_ACCEPTED):
        raise ValueError("Only .xlsx, .xls and .csv files can be imported")
    return lowered


def parse_leads_file(filename: str, content: bytes) -> list[dict[str, str]]:
    """Parse .xlsx/.xls/.csv bytes into header-keyed string rows."""
    lowered = validate_upload_name(filename)
    if len(content) > MAX_IMPORT_BYTES:
        raise ValueError("Import file is too large (5 MB max)")
    if lowered.endswith(".xls"):
        try:
            import xlrd

            book = xlrd.open_workbook(file_contents=content)
            sheet = book.sheet_by_index(0)
            matrix = (
                [sheet.cell_value(r, c) for c in range(sheet.ncols)] for r in range(sheet.nrows)
            )
            return _remap_headers(_rows_from_matrix(list(matrix)))
        except Exception as exc:  # xlrd raises several private error types.
            raise ValueError("The .xls file could not be read") from exc
    return _remap_headers(parse_tabular(filename, content))


def _remap_headers(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    remapped: list[dict[str, str]] = []
    for row in rows:
        out: dict[str, str] = {}
        for key, value in row.items():
            # Upstream canonicalization turns spaces into underscores, so look
            # aliases up in the spaced form as well ("اسم_العميل" -> "اسم العميل").
            spaced = key.replace("_", " ").strip().lower()
            canonical = CRM_HEADER_ALIASES.get(spaced) or CRM_HEADER_ALIASES.get(
                key, _canonical_header(key)
            )
            if canonical == "mobile":
                canonical = "phone"
            out.setdefault(canonical, value)
        remapped.append(out)
    return remapped


@dataclass(frozen=True)
class LeadCandidate:
    row_number: int  # 1-based spreadsheet row (header is row 1)
    full_name: str
    phone: str | None
    email: str | None
    source: str
    notes: str | None


@dataclass(frozen=True)
class RowError:
    row_number: int
    reason: str
    raw: dict[str, str] = field(default_factory=dict)


def normalize_source(raw: str | None) -> str:
    word = (raw or "").strip().lower()
    if not word:
        return CrmLeadSource.IMPORT.value
    for needle, value in _SOURCE_WORDS.items():
        if needle in word:
            return value
    return CrmLeadSource.IMPORT.value


def map_rows(rows: list[dict[str, str]]) -> tuple[list[LeadCandidate], list[RowError]]:
    """Validate + normalize parsed rows; never raise on a bad row."""
    candidates: list[LeadCandidate] = []
    errors: list[RowError] = []
    for index, row in enumerate(rows, start=2):
        full_name = (row.get("full_name") or "").strip()
        if len(full_name) < 2:
            errors.append(RowError(index, "missing_full_name", row))
            continue
        raw_phone = (row.get("phone") or "").strip()
        phone = normalize_mobile(raw_phone) if raw_phone else None
        if raw_phone and phone is None:
            errors.append(RowError(index, "invalid_phone", row))
            continue
        raw_email = (row.get("email") or "").strip().lower()
        email = raw_email or None
        if email and not _EMAIL.fullmatch(email):
            errors.append(RowError(index, "invalid_email", row))
            continue
        if phone is None and email is None:
            errors.append(RowError(index, "missing_contact", row))
            continue
        candidates.append(
            LeadCandidate(
                row_number=index,
                full_name=full_name,
                phone=phone,
                email=email,
                source=normalize_source(row.get("source")),
                notes=(row.get("notes") or "").strip() or None,
            )
        )
    return candidates, errors


def dedupe(
    candidates: list[LeadCandidate],
    existing_phones: set[str],
    existing_emails: set[str],
) -> tuple[list[LeadCandidate], list[RowError], int]:
    """Flag duplicates against the database and inside the file itself."""
    accepted: list[LeadCandidate] = []
    errors: list[RowError] = []
    seen_phones = set(existing_phones)
    seen_emails = {email.lower() for email in existing_emails}
    duplicates = 0
    for candidate in candidates:
        reason: str | None = None
        if candidate.phone and candidate.phone in seen_phones:
            reason = "duplicate_phone"
        elif candidate.email and candidate.email.lower() in seen_emails:
            reason = "duplicate_email"
        if reason:
            duplicates += 1
            errors.append(
                RowError(
                    candidate.row_number,
                    reason,
                    {
                        "full_name": candidate.full_name,
                        "phone": candidate.phone or "",
                        "email": candidate.email or "",
                    },
                )
            )
            continue
        if candidate.phone:
            seen_phones.add(candidate.phone)
        if candidate.email:
            seen_emails.add(candidate.email.lower())
        accepted.append(candidate)
    return accepted, errors, duplicates


def template_rows() -> tuple[list[str], list[list[Any]]]:
    headers = ["full_name", "phone", "email", "source", "notes"]
    rows: list[list[Any]] = [
        ["Mona Ali", "01012345678", "mona@example.com", "whatsapp", "مهتمة بالتأمين الطبي"],
        ["Karim Samy", "+201112345678", "karim@example.com", "call", ""],
    ]
    return headers, rows
