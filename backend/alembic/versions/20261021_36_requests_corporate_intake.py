"""Requests (not complaints), omni-channel intake and the corporate portal.

Revision ID: 20261021_36
Revises: 20261019_35
"""

from __future__ import annotations

from pathlib import Path

import sqlparse

from alembic import op

revision = "20261021_36"
down_revision = "20261019_35"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / "036_requests_corporate_intake.sql"

# Everything after this marker builds indexes on pre-existing hot tables and
# must run CONCURRENTLY, which PostgreSQL forbids inside a transaction block.
CONCURRENT_MARKER = "-- Section 17: concurrent index builds (NON-TRANSACTIONAL)"


def _statements(sql: str) -> list[str]:
    return [
        statement
        for statement in sqlparse.split(sql)
        # sqlparse attaches leading comments to the next statement, so a chunk
        # can start with "--" and still contain real DDL.
        if "\n".join(
            line for line in statement.splitlines() if not line.strip().startswith("--")
        ).strip(" ;\n\t")
    ]


def _index_partitioned_complaints() -> None:
    """Build ix_complaints_corporate without locking out writes.

    ``complaints`` is partitioned by range, and PostgreSQL refuses
    CREATE INDEX CONCURRENTLY on a partitioned parent. The supported
    zero-downtime recipe is to create an empty parent index ON ONLY (instant:
    the parent holds no rows), build each partition's index CONCURRENTLY, then
    ATTACH them. The parent index only becomes valid once every partition is
    attached, so a partial run simply resumes.
    """
    connection = op.get_bind()
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_complaints_corporate"
        " ON ONLY public.complaints (corporate_client_id, created_at DESC)"
    )
    partitions = (
        connection.exec_driver_sql(
            """
        SELECT c.relname
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = p.relnamespace
        WHERE n.nspname = 'public' AND p.relname = 'complaints'
        ORDER BY c.relname
        """
        )
        .scalars()
        .all()
    )
    for partition in partitions:
        child_index = f"{partition}_corporate_idx"[:63]
        op.execute(
            f'CREATE INDEX CONCURRENTLY IF NOT EXISTS "{child_index}"'
            f' ON public."{partition}" (corporate_client_id, created_at DESC)'
        )
        op.execute(
            f'ALTER INDEX public.ix_complaints_corporate ATTACH PARTITION public."{child_index}"'
        )


def upgrade() -> None:
    sql = SQL.read_text(encoding="utf-8")
    transactional, _, concurrent = sql.partition(CONCURRENT_MARKER)

    for statement in _statements(transactional):
        op.execute(statement)

    if concurrent.strip():
        with op.get_context().autocommit_block():
            for statement in _statements(concurrent):
                op.execute(statement)
            _index_partitioned_complaints()


def downgrade() -> None:
    raise RuntimeError(
        "Request intake and corporate contracts are forward-only; restore from backup"
    )
