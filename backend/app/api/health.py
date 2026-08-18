"""Health check endpoint."""

from fastapi import APIRouter

from backend.app.db import ping

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    db_ok = await ping()
    return {"status": "ok", "db": db_ok, "version": "0.1.0"}
