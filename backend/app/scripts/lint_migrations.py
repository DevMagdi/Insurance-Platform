"""Zero-downtime migration linter for SQL revisions from Phase 8 onwards.

Rolling deployments run old and new application versions side by side against
one schema, so every migration must be *expand-only* (backwards compatible) and
must avoid long ``ACCESS EXCLUSIVE`` locks on hot tables. Destructive
"contract" steps belong to a later release after all pods run the new code.

Usage: ``python -m app.scripts.lint_migrations`` (CI runs it in the backend job).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlparse

SQL_DIR = Path(__file__).resolve().parents[2] / "alembic" / "sql"
FIRST_LINTED = 21  # earlier revisions predate the rule and are immutable history

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DROP TABLE/COLUMN breaks the running version", re.compile(r"\bDROP\s+(TABLE|COLUMN)\b")),
    ("RENAME breaks the running version", re.compile(r"\bRENAME\s+(TO|COLUMN|CONSTRAINT)\b")),
    (
        "ALTER COLUMN TYPE rewrites the table",
        re.compile(r"\bALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE\b"),
    ),
    ("SET NOT NULL scans under an exclusive lock", re.compile(r"\bSET\s+NOT\s+NULL\b")),
    ("explicit LOCK TABLE", re.compile(r"\bLOCK\s+TABLE\b")),
    ("VACUUM FULL / CLUSTER rewrite the table", re.compile(r"\b(VACUUM\s+FULL|CLUSTER)\b")),
    ("TRUNCATE destroys data", re.compile(r"\bTRUNCATE\b")),
)
_ADD_NOT_NULL = re.compile(r"\bADD\s+COLUMN\b(?P<body>[^,;]*)", re.IGNORECASE)
_CREATE_TABLE = re.compile(r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)")
_CREATE_INDEX = re.compile(
    r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?P<concurrently>CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?\w+\s+ON\s+(?:ONLY\s+)?(?:public\.)?(?P<table>\w+)"
)
_ADD_FK = re.compile(r"\bADD\s+(CONSTRAINT\s+\w+\s+)?FOREIGN\s+KEY\b(?![^;]*NOT\s+VALID)")


@dataclass(frozen=True)
class Finding:
    file: str
    statement: int
    message: str

    def __str__(self) -> str:
        return f"{self.file} statement {self.statement}: {self.message}"


def _strip_comments(sql: str) -> str:
    return sqlparse.format(sql, strip_comments=True)


def lint_sql(name: str, sql: str) -> list[Finding]:
    findings: list[Finding] = []
    created: set[str] = set()
    for index, raw in enumerate(sqlparse.split(sql), start=1):
        statement = _strip_comments(raw)
        upper = statement.upper()
        # Function bodies are code, not DDL executed by the migration.
        if re.match(r"\s*CREATE\s+(OR\s+REPLACE\s+)?FUNCTION\b", upper):
            continue
        for table in _CREATE_TABLE.findall(statement):
            created.add(table.lower())
        for message, pattern in RULES:
            if pattern.search(upper):
                findings.append(Finding(name, index, message))
        for match in _ADD_NOT_NULL.finditer(statement):
            body = match.group("body").upper()
            if "NOT NULL" in body and "DEFAULT" not in body:
                findings.append(
                    Finding(name, index, "ADD COLUMN NOT NULL needs a constant DEFAULT")
                )
        for match in _CREATE_INDEX.finditer(statement):
            if match.group("table").lower() not in created and not match.group("concurrently"):
                findings.append(
                    Finding(
                        name,
                        index,
                        f"index on existing table {match.group('table')} must be CONCURRENTLY "
                        "(in a non-transactional revision)",
                    )
                )
        if _ADD_FK.search(upper) and not _CREATE_TABLE.search(statement):
            findings.append(
                Finding(name, index, "new FOREIGN KEY on an existing table must be NOT VALID")
            )
    return findings


def linted_files(directory: Path = SQL_DIR) -> list[Path]:
    files = []
    for path in sorted(directory.glob("*.sql")):
        prefix = path.name.split("_", 1)[0]
        if prefix.isdecimal() and int(prefix) >= FIRST_LINTED:
            files.append(path)
    return files


def main() -> int:
    findings = [
        finding
        for path in linted_files()
        for finding in lint_sql(path.name, path.read_text(encoding="utf-8"))
    ]
    for finding in findings:
        print(f"::error title=Unsafe migration::{finding}")
    print(f"checked {len(linted_files())} revision(s); {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
