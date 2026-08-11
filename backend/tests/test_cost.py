"""Test for `GET /runs/{id}/cost` and `GET /runs/{id}/audit` (implementation
plan section 12 `test_cost.py`, task_breakdown Step 30 (4), CLAUDE.md
behavior 10).

Two independent claims, one per endpoint:

  - `get_run_cost`'s per-stage breakdown sums exactly to its own total --
    driven off a real run (classify + extract each bill one FakeLLM call,
    same pattern as test_api_runs.py) rather than hand-inserted CostEvent
    rows, so this is the real `cost_events` aggregation query, not a
    reimplementation of it.
  - `get_run_audit` returns events ordered by `occurred_at`, not merely by
    insertion order -- proven by inserting events with explicit timestamps
    *out of insertion order* (the row committed last carries the earliest
    `occurred_at`) and asserting the response is chronological anyway. A
    test that only checked "the events I inserted in order come back in
    order" couldn't tell time-ordering apart from id-ordering, since a real
    run's events are, in fact, both.

Marked `integration` and skipped when DATABASE_URL is unreachable, matching
every other integration test in this suite.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.app.agent.nodes.classify import SYSTEM_PROMPT as CLASSIFY_SYSTEM_PROMPT
from backend.app.agent.nodes.classify import build_batch_messages
from backend.app.agent.nodes.extract import SYSTEM_PROMPTS as EXTRACT_SYSTEM_PROMPTS
from backend.app.agent.nodes.extract import build_messages as build_extract_messages
from backend.app.agent.state import DocumentRef
from backend.app.db import AsyncSessionLocal, engine
from backend.app.main import app
from backend.app.models import AuditEvent, Corpus, CostEvent, Document, Run
from backend.app.worker import run_once
from backend.tests.fakes import cache_key

pytestmark = pytest.mark.integration


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    from backend.app.config import get_settings
    from backend.app.services import ingestion as ingestion_module

    real_settings = get_settings()
    test_settings = real_settings.model_copy(update={"CORPUS_ROOT": tmp_path})
    monkeypatch.setattr(ingestion_module, "get_settings", lambda: test_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_cost_stages_sum_to_total(api_client, fake_llm):
    corpus_resp = await api_client.post(
        "/corpora", json={"name": "cost-test", "inbox_path": "/tmp/cost-test-inbox"}
    )
    corpus_id = corpus_resp.json()["id"]

    doc_resp = await api_client.post(
        f"/corpora/{corpus_id}/documents",
        files={
            "file": ("prd.md", b"# Feature Cost\n\nOwned by nobody in particular.", "text/markdown")
        },
    )
    document = doc_resp.json()

    async with AsyncSessionLocal() as session:
        persisted = await session.get(Document, document["id"])
        chunks = list(persisted.chunks)

    doc_ref = [DocumentRef(id=persisted.id, filename=persisted.filename)]
    fake_llm._responses[
        cache_key("classify", CLASSIFY_SYSTEM_PROMPT, build_batch_messages(doc_ref))
    ] = {
        "text": json.dumps(
            [{"document_id": str(persisted.id), "doc_type": "prd", "confidence": 0.9}]
        ),
        "input_tokens": 40,
        "output_tokens": 15,
        "stop_reason": "end_turn",
    }
    fake_llm._responses[
        cache_key("extract", EXTRACT_SYSTEM_PROMPTS["prd"], build_extract_messages(chunks))
    ] = {
        "text": "[]",
        "input_tokens": 80,
        "output_tokens": 5,
        "stop_reason": "end_turn",
    }

    run_resp = await api_client.post("/runs", json={"corpus_id": corpus_id, "kind": "initial"})
    run_id = run_resp.json()["id"]

    claimed = await run_once()
    assert str(claimed) == run_id

    cost_resp = await api_client.get(f"/runs/{run_id}/cost")
    assert cost_resp.status_code == 200
    cost = cost_resp.json()

    assert cost["total_usd_cost"] > 0.0
    assert {s["stage"] for s in cost["stages"]} == {"classify", "extract"}
    stage_sum = sum(Decimal(str(s["usd_cost"])) for s in cost["stages"])
    assert stage_sum == Decimal(str(cost["total_usd_cost"]))

    async with AsyncSessionLocal() as session:
        await session.execute(delete(CostEvent).where(CostEvent.run_id == uuid.UUID(run_id)))
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id == uuid.UUID(run_id)))
        await session.execute(delete(Run).where(Run.id == uuid.UUID(run_id)))
        await session.execute(delete(Document).where(Document.corpus_id == uuid.UUID(corpus_id)))
        await session.execute(delete(Corpus).where(Corpus.id == uuid.UUID(corpus_id)))
        await session.commit()


async def test_audit_events_ordered_by_time_not_insertion_order(api_client):
    corpus_resp = await api_client.post(
        "/corpora", json={"name": "audit-order-test", "inbox_path": "/tmp/audit-order-test-inbox"}
    )
    corpus_id = corpus_resp.json()["id"]

    async with AsyncSessionLocal() as session:
        run = Run(corpus_id=uuid.UUID(corpus_id), kind="initial", status="running")
        session.add(run)
        await session.commit()
        await session.refresh(run)
        run_id = run.id

        base = datetime(2026, 1, 1, tzinfo=UTC)
        # Insert "third", then "first", then "second" -- insertion order and
        # id order both disagree with chronological order.
        session.add_all(
            [
                AuditEvent(
                    run_id=run_id,
                    event_type="third",
                    payload={},
                    occurred_at=base + timedelta(seconds=30),
                ),
                AuditEvent(run_id=run_id, event_type="first", payload={}, occurred_at=base),
                AuditEvent(
                    run_id=run_id,
                    event_type="second",
                    payload={},
                    occurred_at=base + timedelta(seconds=15),
                ),
            ]
        )
        await session.commit()

    audit_resp = await api_client.get(f"/runs/{run_id}/audit")
    assert audit_resp.status_code == 200
    event_types = [e["event_type"] for e in audit_resp.json()]
    assert event_types == ["first", "second", "third"]

    async with AsyncSessionLocal() as session:
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id == run_id))
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(Corpus).where(Corpus.id == uuid.UUID(corpus_id)))
        await session.commit()
