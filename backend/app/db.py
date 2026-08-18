"""Shared async engine, session factory, and FastAPI session dependency.

State, vectors, audit, and cost all live in this one Postgres instance
(see CLAUDE.md architectural rules) so every part of the app that touches
the database goes through the engine defined here.
"""

from collections.abc import AsyncGenerator

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.config import get_settings

logger = structlog.get_logger(__name__)

settings = get_settings()

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def ping() -> bool:
    """Run SELECT 1 against the database, returning False on any failure."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.warning("db_ping_failed", exc_info=True)
        return False
