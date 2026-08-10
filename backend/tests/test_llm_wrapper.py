"""Tests for backend.app.services.llm, exercised through FakeLLM.

Requires no ANTHROPIC_API_KEY (fake_llm fixture never constructs a real
AnthropicProvider). Persisting cost_events rows needs a live Postgres, so
those tests are marked `integration` and skip when DATABASE_URL is
unreachable, matching test_db_ping.py's pattern.
"""

from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from backend.app.config import get_settings
from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import Corpus, CostEvent, Run
from backend.app.services.llm import BudgetExceeded, call_claude
from backend.tests.fakes import FakeLLM, MissingFixtureError

SYSTEM = "You are a test system prompt."
MESSAGES = [{"role": "user", "content": "unrecorded call, no fixture for this"}]

CLASSIFY_SYSTEM = (
    "You are a document classifier for a PM analyst intake pipeline. Read the document "
    'text and respond with a JSON object: {"category": one of ["prd", "tech_spec", '
    '"ticket", "meeting_notes", "release_notes"], "confidence": 0.0-1.0}.'
)
CLASSIFY_MESSAGES = [
    {
        "role": "user",
        "content": "Classify this document:\n\nSprint 14 planning notes: decided to cut "
        "SSO from MVP, revisit in Q3.",
    }
]


async def test_fake_llm_raises_on_missing_fixture():
    fake = FakeLLM()
    with pytest.raises(MissingFixtureError) as exc_info:
        await fake.complete(SYSTEM, MESSAGES, None, "claude-sonnet-5", "classify")

    message = str(exc_info.value)
    assert "classify:" in message
    assert "no fixture recorded" in message
    assert fake.calls[0].stage == "classify"


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def run_id():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="llm-wrapper-test", inbox_path="/tmp/llm-wrapper-test-inbox")
        session.add(corpus)
        await session.flush()
        run = Run(corpus_id=corpus.id, kind="initial", status="running")
        session.add(run)
        await session.commit()
        cid, rid = corpus.id, run.id

    yield rid

    async with AsyncSessionLocal() as session:
        await session.execute(delete(CostEvent).where(CostEvent.run_id == rid))
        await session.execute(delete(Run).where(Run.id == rid))
        await session.execute(delete(Corpus).where(Corpus.id == cid))
        await session.commit()


@pytest.mark.integration
async def test_call_claude_records_cost_event(run_id, fake_llm):
    async with AsyncSessionLocal() as session:
        result = await call_claude(session, run_id, "classify", CLASSIFY_SYSTEM, CLASSIFY_MESSAGES)

    assert result.text == '{"category": "meeting_notes", "confidence": 0.92}'
    assert fake_llm.calls[0].stage == "classify"

    async with AsyncSessionLocal() as session:
        events = (await session.scalars(select(CostEvent).where(CostEvent.run_id == run_id))).all()

    assert len(events) == 1
    event = events[0]
    assert event.stage == "classify"
    assert event.input_tokens == 118
    assert event.output_tokens == 14
    assert event.usd_cost > Decimal(0)


@pytest.mark.integration
async def test_call_claude_raises_budget_exceeded(run_id, fake_llm, monkeypatch):
    monkeypatch.setenv("COST_BUDGET_USD_PER_RUN", "0.0001")
    get_settings.cache_clear()
    try:
        async with AsyncSessionLocal() as session:
            with pytest.raises(BudgetExceeded):
                await call_claude(session, run_id, "classify", CLASSIFY_SYSTEM, CLASSIFY_MESSAGES)
    finally:
        get_settings.cache_clear()

    # The budget check happens before the provider is called and before any
    # cost_events row is written.
    assert fake_llm.calls == []
    async with AsyncSessionLocal() as session:
        events = (await session.scalars(select(CostEvent).where(CostEvent.run_id == run_id))).all()
    assert events == []
