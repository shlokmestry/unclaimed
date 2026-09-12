# Async engine/session, used by the FastAPI app (Step 6 asks for a
# SQLAlchemy async session). Ingestion stays on the sync engine in
# db/session.py — it's a sequential script, not a request handler, so there's
# no benefit to async there.

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from db.config import ASYNC_DATABASE_URL

# Running against Supabase's Transaction pooler (PgBouncer in transaction
# mode): a single client connection gets handed off to a *different* backend
# Postgres process between transactions, but two separate caches assumed a
# stable 1:1 mapping between a connection and its prepared statements, so
# both had to go:
#
# 1. `statement_cache_size=0` disables asyncpg's own client-side prepared
#    statement cache.
# 2. `prepared_statement_cache_size=0` disables a *second*, separate cache
#    that SQLAlchemy's asyncpg dialect keeps on top of that (see "Prepared
#    Statement Cache" in the SQLAlchemy asyncpg dialect docs) — this was the
#    one actually causing the "number of columns in the result row (18) is
#    different from what was described (1)" errors: a stale cached
#    PreparedStatement object from one query got served for another.
# 3. Even with both caches off, asyncpg still names each prepared statement
#    itself using a simple incrementing counter ("__asyncpg_stmt_5__"), and
#    since PgBouncer transaction-mode connections aren't reset between
#    transactions (no DISCARD), two different backend-routed transactions can
#    independently arrive at the same generated name and collide
#    ("already exists" / "does not exist"). `prepared_statement_name_func`
#    (a SQLAlchemy/asyncpg-documented workaround for exactly this) makes each
#    name globally unique instead.
# 4. `poolclass=NullPool` avoids holding pooled connections open across
#    requests (recommended alongside the above so leftover prepared
#    statements don't accumulate on the PgBouncer/Postgres side).
async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    future=True,
    poolclass=NullPool,
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    },
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine, class_=AsyncSession, autoflush=False, autocommit=False, expire_on_commit=False
)


async def get_session() -> AsyncSession:
    """FastAPI dependency that yields an AsyncSession per request."""
    async with AsyncSessionLocal() as session:
        yield session
