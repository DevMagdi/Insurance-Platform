"""Celery app for Phase 1/2 periodic work (spec stack: FastAPI+PG+Redis+Celery).

Beat schedule:
- due CRM task reminders stamped hourly (unified CRM, ``crm_tasks``);
- premium installments flipped to ``overdue`` daily;
- policy renewal alerts armed daily (30 days ahead, once per policy).

The pre-existing ARQ worker keeps the transactional outbox/scan pipeline it
already owns; Celery owns the CRM/policy lifecycle jobs added in Phase 2, and
both share Redis. Tasks authenticate to PostgreSQL as the table-blind
``app_worker`` login and set the ``worker`` actor scope so RLS still applies.
"""

from __future__ import annotations

import logging
import os

from celery import Celery
from celery.schedules import crontab
from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

celery_app = Celery(
    "insurance_platform",
    broker=os.environ.get("CELERY_BROKER_URL", os.environ.get("REDIS_URL", "redis://redis:6379/0")),
    backend=os.environ.get(
        "CELERY_RESULT_BACKEND", os.environ.get("REDIS_URL", "redis://redis:6379/0")
    ),
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    timezone="Africa/Cairo",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "crm-task-reminders": {
            "task": "app.celery_app.crm_task_reminders",
            "schedule": crontab(minute=5),
        },
        "installments-overdue": {
            "task": "app.celery_app.mark_overdue_installments",
            "schedule": crontab(hour=1, minute=15),
        },
        "policy-renewal-alerts": {
            "task": "app.celery_app.policy_renewal_alerts",
            "schedule": crontab(hour=8, minute=0),
        },
    },
)


def _engine() -> object:
    url = os.environ["WORKER_DATABASE_URL"].replace("+asyncpg", "+psycopg")
    return create_engine(url)


def _run(sql: str) -> int:
    engine = _engine()
    try:
        with engine.begin() as connection:  # type: ignore[attr-defined]
            connection.execute(text("SELECT set_config('app.scope', 'worker', true)"))
            result = connection.execute(text(sql))
            return result.rowcount or 0
    finally:
        engine.dispose()  # type: ignore[attr-defined]


@celery_app.task(name="app.celery_app.crm_task_reminders")  # type: ignore[untyped-decorator]
def crm_task_reminders() -> dict[str, int]:
    """Stamp due CRM tasks for reminding and count leads awaiting follow-up.

    Idempotent: only pending tasks whose reminder was never sent are
    stamped; the Tasks tab badges read the same rows.
    """
    reminders = _run(
        "UPDATE crm_tasks SET reminder_sent_at = now(), updated_at = now()"
        " WHERE status = 'pending' AND due_at <= now() AND reminder_sent_at IS NULL"
    )
    engine = _engine()
    try:
        with engine.connect() as connection:  # type: ignore[attr-defined]
            connection.execute(text("SELECT set_config('app.scope', 'worker', true)"))
            due = (
                connection.execute(
                    text(
                        "SELECT count(*) FROM crm_leads WHERE next_followup_at IS NOT NULL"
                        " AND next_followup_at <= now()"
                        " AND converted_at IS NULL"
                    )
                ).scalar()
                or 0
            )
    finally:
        engine.dispose()  # type: ignore[attr-defined]
    logger.info("crm task reminders stamped: %s (leads due: %s)", reminders, due)
    return {"reminders": reminders, "due_leads": int(due)}


@celery_app.task(name="app.celery_app.mark_overdue_installments")  # type: ignore[untyped-decorator]
def mark_overdue_installments() -> dict[str, int]:
    changed = _run(
        "UPDATE policy_installments SET status = 'overdue', updated_at = now()"
        " WHERE status = 'pending' AND due_date < CURRENT_DATE"
    )
    logger.info("installments marked overdue: %s", changed)
    return {"overdue": changed}


@celery_app.task(name="app.celery_app.policy_renewal_alerts")  # type: ignore[untyped-decorator]
def policy_renewal_alerts() -> dict[str, int]:
    """Arm the renewal alert once, 30 days before a policy ends (idempotent)."""
    changed = _run(
        "UPDATE brokerage_policies SET renewal_alert_sent_at = now(), updated_at = now()"
        " WHERE status = 'active' AND renewal_alert_sent_at IS NULL"
        " AND ends_on <= CURRENT_DATE + INTERVAL '30 days' AND ends_on >= CURRENT_DATE"
    )
    logger.info("policy renewal alerts armed: %s", changed)
    return {"alerts": changed}
