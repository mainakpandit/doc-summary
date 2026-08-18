"""Tests for the test harness itself (backend/tests/conftest.py): the
Postgres+pgvector container, the Alembic migrations applied to it, and the
db_session / async_client fixtures built on top.

Requires no ANTHROPIC_API_KEY. Requires Docker (conftest.py's
pytest_configure spins up the container for the whole session), so these
are not marked `integration` / skip-on-unreachable like the older
per-test-reachability-checked tests -- if the container fails to start,
every test in the session fails loudly instead of silently skipping.
"""

from sqlalchemy import select, text

from backend.app.models import Corpus


async def test_db_session_rolls_back_and_does_not_leak_between_tests(db_session):
    before = await db_session.scalar(select(Corpus))
    assert before is None, "a previous test's data leaked into this one"

    db_session.add(Corpus(name="harness-pollution-check", inbox_path="/tmp/harness-inbox"))
    await db_session.flush()

    seen = await db_session.scalar(select(Corpus).where(Corpus.name == "harness-pollution-check"))
    assert seen is not None, "expected to see the row within the same test/transaction"


async def test_second_test_does_not_see_the_first_tests_row(db_session):
    leaked = await db_session.scalar(select(Corpus).where(Corpus.name == "harness-pollution-check"))
    assert leaked is None, (
        "the previous test's Corpus row is visible here -- db_session's "
        "per-test rollback did not isolate the two tests"
    )


async def test_migrations_produce_all_expected_tables(db_engine):
    expected_tables = {
        "runs",
        "corpora",
        "documents",
        "chunks",
        "claims",
        "claim_sources",
        "conflicts",
        "findings",
        "finding_sources",
        "register_entries",
        "register_field_sources",
        "reviews",
        "audit_events",
        "cost_events",
        "alembic_version",
    }

    async with db_engine.connect() as connection:
        result = await connection.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        actual_tables = {row[0] for row in result}

    missing = expected_tables - actual_tables
    assert not missing, f"migrations did not create expected tables: {missing}"


async def test_async_client_health_endpoint_sees_the_migrated_db(async_client):
    response = await async_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] is True
