# Async engine/session, used by the FastAPI app (Step 6 asks for a
# SQLAlchemy async session). Ingestion stays on the sync engine in
# db/session.py — it's a sequential script, not a request handler, so there's
# no benefit to async there.

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.config import ASYNC_DATABASE_URL

# statement_cache_size=0 disables asyncpg's server-side prepared statement
# cache. Required when DATABASE_URL points at a PgBouncer/Supavisor
# connection pooler in transaction-pooling mode (e.g. Supabase's "Transaction
# pooler", or Railway/Neon poolers) — in that mode a prepared statement can
# be created on one physical connection and then reused on a different one
# for a later query in the same session, since the pooler multiplexes
# connections per-transaction rather than per-session. asyncpg's prepared
# statements are tied to the physical connection they were created on, so
# without this they fail intermittently with
# "prepared statement ... does not exist". Harmless against a direct
# (non-pooled) Postgres connection too, just a little less efficient.
async_engine = create_async_engine(
    ASYNC_DATABASE_URL, future=True, connect_args={"statement_cache_size": 0}
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine, class_=AsyncSession, autoflush=False, autocommit=False, expire_on_commit=False
)


async def get_session() -> AsyncSession:
    """FastAPI dependency that yields an AsyncSession per request."""
    async with AsyncSessionLocal() as session:
        yield session
