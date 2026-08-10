"""Shared pytest fixtures.

`fake_llm` and `fake_embedder` monkeypatch the swappable provider
factories in services/llm.py and services/embeddings.py so tests get
deterministic, offline doubles (backend/tests/fakes.py) instead of the
real Anthropic/embedding providers. Neither fixture requires
ANTHROPIC_API_KEY (CLAUDE.md behavior 7).

Database: `pytest_configure` below spins up one Postgres+pgvector
container per test session and applies every Alembic migration to it
before test collection begins. That ordering matters: backend.app.db
builds its module-level `engine` once, at import time, from
`get_settings().DATABASE_URL` (see backend/app/db.py) — so DATABASE_URL
has to point at the container, and the settings cache has to be cleared,
before anything imports that module. `db_session` then gives each test
its own SAVEPOINT-nested transaction (rolled back on exit, even across a
service's internal `session.commit()` calls) bound to `db_engine`, and
`async_client` wires that same session into the FastAPI app through a
`get_session` dependency override.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from testcontainers.postgres import PostgresContainer

from backend.app.services import embeddings as embeddings_service
from backend.app.services import llm as llm_service
from backend.tests.fakes import FakeEmbedder, FakeLLM

REPO_ROOT = Path(__file__).resolve().parents[2]

_postgres_container: PostgresContainer | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Start the session's Postgres+pgvector container and migrate it.

    Runs as a pytest hook (not a fixture) specifically so it executes
    before test collection: test modules like test_db_ping.py import
    `backend.app.db` at module scope, and that import is what freezes the
    engine's connection string, so DATABASE_URL must be pointed at the
    container before collection ever reaches those imports.
    """
    global _postgres_container

    container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
    container.start()
    _postgres_container = container

    os.environ["DATABASE_URL"] = container.get_connection_url()

    # get_settings() is lru_cached; if anything called it before the line
    # above (e.g. a provider imported at module scope during this file's
    # own imports), the cached Settings would still hold the pre-container
    # DATABASE_URL. Clearing here forces the next call to re-read the env.
    from backend.app.config import get_settings

    get_settings.cache_clear()

    _upgrade_head()


def pytest_unconfigure(config: pytest.Config) -> None:
    if _postgres_container is not None:
        _postgres_container.stop()


def _upgrade_head() -> None:
    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(alembic_cfg, "head")


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm_service, "_provider_factory", lambda: fake)
    return fake


@pytest.fixture
def fake_embedder(monkeypatch) -> FakeEmbedder:
    fake = FakeEmbedder()
    monkeypatch.setattr(embeddings_service, "_provider_factory", lambda: fake)
    return fake


@pytest.fixture(scope="session")
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    """The application's own engine (backend.app.db.engine), imported here
    -- not at module scope -- so nothing in this file touches backend.app.db
    before pytest_configure has pointed DATABASE_URL at the container."""
    from backend.app.db import engine

    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """One SAVEPOINT-nested transaction per test, rolled back on exit.

    join_transaction_mode="create_savepoint" is SQLAlchemy's documented
    recipe for joining a Session into an already-open external
    transaction: a service's own `await session.commit()` (several do
    commit internally, e.g. embed_chunks, call_claude) only releases and
    reopens the savepoint, while the real transaction on `connection` is
    never committed -- only rolled back in the `finally` below -- so
    nothing a test does is visible to any other test.
    """
    async with db_engine.connect() as connection:
        await connection.begin()
        session_factory = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        session = session_factory()
        try:
            yield session
        finally:
            await session.close()
            await connection.rollback()


@pytest.fixture
async def async_client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """httpx.AsyncClient bound to the FastAPI app in-process (ASGITransport,
    no real socket), with `get_session` overridden to hand out this test's
    `db_session` so route handlers see the same rolled-back-on-exit
    transaction as the test itself."""
    from backend.app.db import get_session
    from backend.app.main import app

    async def _get_session_override() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_session] = _get_session_override
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        del app.dependency_overrides[get_session]
