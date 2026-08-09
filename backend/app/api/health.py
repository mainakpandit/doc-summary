"""Health check endpoint."""

import structlog
from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from backend.app.config import get_settings

logger = structlog.get_logger(__name__)

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    db_ok = False
    # No shared db.py yet, so the engine is created and torn down inline
    # just for this check; NullPool avoids leaving a pool around after dispose.
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        logger.warning("health_db_check_failed", exc_info=True)
    finally:
        await engine.dispose()

    return {"status": "ok", "db": db_ok, "version": "0.1.0"}
