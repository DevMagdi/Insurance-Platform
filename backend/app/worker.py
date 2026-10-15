"""Restricted periodic outbox delivery and fail-closed virus-scan hook."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from io import BytesIO
from typing import Any, ClassVar
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings
from arq.cron import CronJob
from pydantic import Field
from pydantic_settings import BaseSettings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db import make_worker_engine
from app.services.exports import render_export
from app.services.providers import Delivery, SMTPDelivery, UnconfiguredVirusScanner, VirusScanner
from app.services.storage import ObjectStorage, S3ObjectStorage

logger = logging.getLogger(__name__)


class WorkerConfig(BaseSettings):
    worker_database_url: str = Field(validation_alias="WORKER_DATABASE_URL")
    redis_url: str = "redis://redis:6379/0"
    s3_bucket: str = "complaint-attachments"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_region: str = "us-east-1"
    smtp_host: str | None = None
    smtp_port: int = 1025
    smtp_from: str = "no-reply@insurance.local"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_start_tls: bool = False
    dev_sms_mailbox: bool = False
    environment: str = "production"


async def startup(ctx: dict[str, Any]) -> None:
    config = WorkerConfig()  # type: ignore[call-arg]
    ctx["engine"] = make_worker_engine(config.worker_database_url)
    ctx["delivery"] = SMTPDelivery(config)
    ctx["scanner"] = UnconfiguredVirusScanner()
    ctx["storage"] = (
        S3ObjectStorage(config)
        if config.s3_endpoint_url or (config.s3_access_key_id and config.s3_secret_access_key)
        else None
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    engine: AsyncEngine = ctx["engine"]
    await engine.dispose()


async def _worker_query(engine: AsyncEngine, query: str, params: dict[str, Any]) -> list[Any]:
    async with engine.begin() as connection:
        await connection.execute(text("SELECT set_config('app.scope', 'worker', true)"))
        result = await connection.execute(text(query), params)
        return list(result.all())


async def health_job(ctx: dict[str, Any]) -> str:
    return "ok"


async def deliver_notices(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    delivery: Delivery = ctx["delivery"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.claim_notices(:limit)", {"limit": 20}
    )
    for row in rows:
        notice_id: UUID = row.notice_id
        payload = json.loads(row.payload) if isinstance(row.payload, str) else row.payload
        try:
            delivered = await delivery.send_notice(row.mobile, row.email, row.kind, payload)
            state = "sent" if delivered else "pending_provider"
        except Exception:
            state = "retry"
        await _worker_query(
            engine,
            "SELECT app_security.finish_notice(:id, :state)",
            {"id": notice_id, "state": state},
        )
    return f"processed {len(rows)} notice(s)"


async def assign_tickets(ctx: dict[str, Any]) -> str:
    rows = await _worker_query(
        ctx["engine"], "SELECT app_security.auto_assign_tickets(:limit) AS processed", {"limit": 20}
    )
    return f"assigned {rows[0].processed} ticket(s)"


async def check_sla(ctx: dict[str, Any]) -> str:
    rows = await _worker_query(
        ctx["engine"],
        "SELECT app_security.process_sla_breaches(:limit) AS processed",
        {"limit": 20},
    )
    return f"breached {rows[0].processed} SLA deadline(s)"


async def deliver_insurer(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    delivery: Delivery = ctx["delivery"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.claim_insurer_notices(:limit)", {"limit": 20}
    )
    for row in rows:
        try:
            delivered = await delivery.send_insurer(row.recipient_email, row.ticket_no, row.reason)
            state = "sent" if delivered else "pending_provider"
        except Exception:
            state = "retry"
        await _worker_query(
            engine,
            "SELECT app_security.finish_insurer_notice(:id, :state)",
            {"id": row.notice_id, "state": state},
        )
    return f"processed {len(rows)} insurer notice(s)"


async def scan_pending(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    scanner: VirusScanner = ctx["scanner"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.pending_scans(:limit)", {"limit": 20}
    )
    for row in rows:
        verdict = await scanner.scan(row.storage_key)
        if verdict in ("clean", "infected", "error"):
            await _worker_query(
                engine,
                "SELECT app_security.record_scan_verdict(:id, :verdict)",
                {"id": row.upload_id, "verdict": verdict},
            )
    return f"checked {len(rows)} pending scan(s)"


async def export_pending(ctx: dict[str, Any]) -> str:
    engine: AsyncEngine = ctx["engine"]
    storage: ObjectStorage | None = ctx.get("storage")
    if storage is None:
        return "export storage unavailable; jobs remain queued"
    jobs = await _worker_query(
        engine, "SELECT * FROM app_security.claim_exports(:limit)", {"limit": 10}
    )
    for job in jobs:
        key = f"exports/{job.job_id}/{job.attempt}.csv"
        uploaded = False
        try:
            rows = await _worker_query(
                engine,
                "SELECT * FROM app_security.export_rows(:id, :attempt)",
                {"id": job.job_id, "attempt": job.attempt},
            )
            data = render_export(job.kind, rows)
            manifest = [
                {"id": str(row.complaint_id), "created_at": row.complaint_created_at.isoformat()}
                for row in rows
            ]
            await storage.put(key, BytesIO(data), "text/csv; charset=utf-8")
            uploaded = True
            finished = await _worker_query(
                engine,
                "SELECT app_security.finish_export(:id,:attempt,true,:key,"
                "CAST(:manifest AS jsonb)) AS finished",
                {
                    "id": job.job_id,
                    "attempt": job.attempt,
                    "key": key,
                    "manifest": json.dumps(manifest),
                },
            )
            if not finished[0].finished:
                await storage.delete(key)
        except Exception:
            if uploaded:
                # Private artifact stays unreferenced, even when cleanup fails.
                with suppress(Exception):
                    await storage.delete(key)
            await _worker_query(
                engine,
                "SELECT app_security.finish_export(:id,:attempt,false,NULL,"
                "'[]'::jsonb) AS finished",
                {"id": job.job_id, "attempt": job.attempt},
            )
    return f"processed {len(jobs)} export job(s)"


async def clean_expired_exports(ctx: dict[str, Any]) -> str:
    storage: ObjectStorage | None = ctx.get("storage")
    if storage is None:
        return "export storage unavailable; expired objects remain private"
    engine: AsyncEngine = ctx["engine"]
    rows = await _worker_query(
        engine, "SELECT * FROM app_security.claim_export_cleanup(:limit)", {"limit": 20}
    )
    for row in rows:
        try:
            await storage.delete(row.storage_key)
        except Exception:
            logger.warning("Expired private export object cleanup failed; retrying later")
            continue  # Remains private and marked expired; retry next maintenance run.
        await _worker_query(
            engine,
            "SELECT app_security.finish_export_cleanup(:id,:key)",
            {"id": row.job_id, "key": row.storage_key},
        )
    return f"cleaned {len(rows)} expired export object(s)"


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Awaitable[str]]]] = [health_job]
    cron_jobs: ClassVar[list[CronJob]] = [
        cron(deliver_notices, second=0, run_at_startup=True),
        cron(assign_tickets, second=5, run_at_startup=True),
        cron(scan_pending, second=15, run_at_startup=True),
        cron(check_sla, second=20, run_at_startup=True),
        cron(deliver_insurer, second=30, run_at_startup=True),
        cron(export_pending, second=45, run_at_startup=True),
        cron(clean_expired_exports, minute={0, 15, 30, 45}, second=55, run_at_startup=True),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(os.getenv("REDIS_URL", "redis://redis:6379/0"))
    max_jobs = 20
    job_timeout = 120
