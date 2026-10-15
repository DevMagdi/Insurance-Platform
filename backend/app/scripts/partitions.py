"""Create upcoming monthly partitions *before* data can land in DEFAULT.

Run with DATABASE_ADMIN_URL and a migration-capable role. If DEFAULT already has
rows for a new range, PostgreSQL refuses the split; move them in a maintenance
window before retrying (do not silently lose complaint data).
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

import psycopg
from psycopg import sql

PARENTS = ("complaints", "complaint_events", "audit_logs")


def add_months(day: date, count: int) -> date:
    index = day.year * 12 + day.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def ensure_partitions(connection: psycopg.Connection[object], months_ahead: int = 6) -> None:
    if not 0 <= months_ahead <= 36:
        raise ValueError("months_ahead must be between 0 and 36")
    current = datetime.now(UTC).date().replace(day=1)
    for offset in range(months_ahead + 1):
        start = add_months(current, offset)
        end = add_months(start, 1)
        lower = f"{start.isoformat()} 00:00:00+00"
        upper = f"{end.isoformat()} 00:00:00+00"
        for parent in PARENTS:
            name = f"{parent}_{start:%Y%m}"
            connection.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})"
                ).format(
                    sql.Identifier(name),
                    sql.Identifier(parent),
                    sql.Literal(lower),
                    sql.Literal(upper),
                )
            )


def main() -> None:
    url = os.environ["DATABASE_ADMIN_URL"]
    with psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1)) as conn:
        ensure_partitions(conn)
    print("Monthly partitions checked for current month through six months ahead")


if __name__ == "__main__":
    main()
