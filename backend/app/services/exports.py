"""Small bounded artifacts; no customer PII in analytics exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.services.complaints import safe_csv_cell
from app.services.render import Cell, tabular_bytes

MAX_EXPORT_ROWS = 1000
MAX_EXPORT_BYTES = 2_000_000
COMPLAINT_COLUMNS = (
    "ticket_no",
    "created_at",
    "company_id",
    "insurance_type",
    "status",
    "subject",
    "policy_number",
)
ANALYTICS_COLUMNS = ("company_id", "total", "sla_breaches", "avg_resolution_hours", "csat_avg")


def export_table(kind: str, rows: Sequence[Any]) -> list[list[Cell]]:
    if kind == "complaints":
        return [
            [
                row.ticket_no,
                row.created_at,
                str(row.company_id),
                row.insurance_type,
                row.status,
                safe_csv_cell(row.subject),
                safe_csv_cell(row.policy_number),
            ]
            for row in rows
        ]
    if kind != "analytics":
        raise ValueError("Unsupported export kind")
    # These rows deliberately have no subject, policy number or ticket number.
    groups: dict[str, dict[str, float | int]] = {}
    for row in rows:
        group = groups.setdefault(
            str(row.company_id),
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
            group["resolution_hours"] += (row.resolved_at - row.created_at).total_seconds() / 3600
        if row.csat_score is not None:
            group["csat_count"] += 1
            group["csat_total"] += row.csat_score
    table: list[list[Cell]] = []
    for company, group in sorted(groups.items()):
        resolved = int(group["resolved"])
        csat_count = int(group["csat_count"])
        table.append(
            [
                company,
                int(group["total"]),
                int(group["sla_breaches"]),
                round(group["resolution_hours"] / resolved, 2) if resolved else "",
                round(group["csat_total"] / csat_count, 2) if csat_count else "",
            ]
        )
    return table


def render_export(kind: str, rows: Sequence[Any], fmt: str = "csv") -> bytes:
    if len(rows) > MAX_EXPORT_ROWS:
        raise ValueError("Export exceeds the 1000-ticket safety limit")
    columns = COMPLAINT_COLUMNS if kind == "complaints" else ANALYTICS_COLUMNS
    data = tabular_bytes(fmt, f"export-{kind}", columns, export_table(kind, rows))
    if len(data) > MAX_EXPORT_BYTES:
        raise ValueError("Export exceeds the 2 MB safety limit")
    return data
