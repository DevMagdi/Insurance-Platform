"""Small bounded CSV artifacts; no customer PII in analytics exports."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from typing import Any

from app.services.complaints import safe_csv_cell

MAX_EXPORT_ROWS = 1000
MAX_EXPORT_BYTES = 2_000_000


def render_export(kind: str, rows: Sequence[Any]) -> bytes:
    if len(rows) > MAX_EXPORT_ROWS:
        raise ValueError("Export exceeds the 1000-ticket safety limit")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    if kind == "complaints":
        writer.writerow(
            (
                "ticket_no",
                "created_at",
                "company_id",
                "insurance_type",
                "status",
                "subject",
                "policy_number",
            )
        )
        for row in rows:
            writer.writerow(
                (
                    row.ticket_no,
                    row.created_at.isoformat(),
                    str(row.company_id),
                    row.insurance_type,
                    row.status,
                    safe_csv_cell(row.subject),
                    safe_csv_cell(row.policy_number),
                )
            )
    elif kind == "analytics":
        # These rows deliberately have no subject, policy number or ticket number.
        # The worker groups them by company before anything is written to storage.
        groups: dict[str, dict[str, float | int]] = {}
        for row in rows:
            company = str(row.company_id)
            group = groups.setdefault(
                company,
                {
                    "total": 0,
                    "sla_breaches": 0,
                    "resolved": 0,
                    "resolution_hours": 0.0,
                    "csat_count": 0,
                    "csat_total": 0,
                },
            )
            group["total"] += 1
            group["sla_breaches"] += int(row.sla_breached)
            if row.resolved_at is not None:
                group["resolved"] += 1
                group["resolution_hours"] += (
                    row.resolved_at - row.created_at
                ).total_seconds() / 3600
            if row.csat_score is not None:
                group["csat_count"] += 1
                group["csat_total"] += row.csat_score
        writer.writerow(
            (
                "company_id",
                "total",
                "sla_breaches",
                "avg_resolution_hours",
                "csat_avg",
            )
        )
        for company, group in sorted(groups.items()):
            writer.writerow(
                (
                    company,
                    int(group["total"]),
                    int(group["sla_breaches"]),
                    round(group["resolution_hours"] / group["resolved"], 2)
                    if group["resolved"]
                    else "",
                    round(group["csat_total"] / group["csat_count"], 2)
                    if group["csat_count"]
                    else "",
                )
            )
    else:
        raise ValueError("Unsupported export kind")
    data = buffer.getvalue().encode("utf-8")
    if len(data) > MAX_EXPORT_BYTES:
        raise ValueError("Export exceeds the 2 MB safety limit")
    return data
