"""Integration test for backend.app.db.ping().

Skips instead of failing when no database is reachable, since the test
harness fixtures for spinning one up (CLAUDE.md behavior 7) haven't landed
yet. Requires no ANTHROPIC_API_KEY.
"""

import pytest

from backend.app.db import engine, ping


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.mark.integration
async def test_ping_returns_true_against_live_db():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")
    assert await ping() is True
