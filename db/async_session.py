# Async engine/session, used by the FastAPI app (Step 6 asks for a
# SQLAlchemy async session). Ingestion stays on the sync engine in
# db/session.py — it's a sequential script, not a request handler, so there's
# no benefit to async there.

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.config import ASYNC_DATABASE_URL

async_engine = create_async_engine(ASYNC_DATABASE_URL, future=True)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine, class_=AsyncSession, autoflush=False, autocommit=False, expire_on_commit=False
)


async def get_session() -> AsyncSession:
    """FastAPI dependency that yields an AsyncSession per request."""
    async with AsyncSessionLocal() as session:
        yield session
