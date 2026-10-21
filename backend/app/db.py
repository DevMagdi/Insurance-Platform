"""Transaction-scoped identity for RLS, safe with PgBouncer transaction pooling."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

ActorScope = Literal["staff", "customer", "corporate"]
PurposeScope = Literal[
    "staff_auth",
    "customer_auth",
    "corporate_auth",
    "invitation",
    "guest",
    "webhook",
]


@dataclass(frozen=True)
class Actor:
    id: UUID
    scope: ActorScope


def make_engine(url: str) -> AsyncEngine:
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("Runtime DATABASE_URL must use postgresql+asyncpg://")
    if make_url(url).username != "app_runtime":
        raise ValueError("Runtime DATABASE_URL must use the non-BYPASSRLS app_runtime role")
    # Never carry a session across requests, or cache named prepared statements on a
    # connection that PgBouncer may reassign to another backend after COMMIT.
    return create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__insurance_stmt_{uuid4()}__",
        },
    )


def make_worker_engine(url: str) -> AsyncEngine:
    """A separate, table-blind DB login for queued delivery and scan callbacks."""
    if not url.startswith("postgresql+asyncpg://") or make_url(url).username != "app_worker":
        raise ValueError("Worker URL must use the restricted app_worker role")
    return create_async_engine(
        url,
        poolclass=NullPool,
        connect_args={
            "prepared_statement_cache_size": 0,
            "statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__insurance_worker_{uuid4()}__",
        },
    )


@asynccontextmanager
async def scoped_transaction(
    factory: async_sessionmaker[AsyncSession], actor: Actor, *, read_only: bool = False
) -> AsyncIterator[AsyncSession]:
    """A caller-authenticated actor; never accept an actor ID from request payloads.

    SET LOCAL is effective only until this transaction commits/rolls back. Both
    GUCs and all queries MUST use this same connection inside this block.
    ``read_only`` is used for replica-routed analytics (RLS applies unchanged).
    """
    async with factory() as session, session.begin():
        if read_only:
            await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(
            text(
                "SELECT set_config('app.user_id', :actor_id, true), "
                "set_config('app.scope', :actor_scope, true)"
            ),
            {"actor_id": str(actor.id), "actor_scope": actor.scope},
        )
        yield session


@asynccontextmanager
async def purpose_transaction(
    factory: async_sessionmaker[AsyncSession], purpose: PurposeScope
) -> AsyncIterator[AsyncSession]:
    """Only for narrowly scoped SECURITY DEFINER login/guest functions.

    No table has a SELECT/INSERT RLS policy for these unauthenticated scopes.
    The API sets the purpose; it cannot be supplied by request data.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "SELECT set_config('app.user_id', '', true), set_config('app.scope', :scope, true)"
            ),
            {"scope": purpose},
        )
        yield session
