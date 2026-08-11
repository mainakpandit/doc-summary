"""Tests for the `/corpora`, `/documents`, and `/runs` API surface
(task_breakdown Step 24).

Drives a full flow purely through `httpx.AsyncClient` against the real
FastAPI app -- create a corpus, upload a document, start a run, then
"restart the worker" the same way `test_agent_resume.py` does by calling
`backend.app.worker.run_once()` directly -- and asserts:

  - the upload persists a `Document` (and, transitively, `Chunk` rows) via
    `services.ingestion.ingest_file`
  - `POST /runs` honors `Idempotency-Key`: a repeat with the same key
    returns the same run rather than creating a second one
  - `GET /runs/{id}` reports a terminal status and per-run counts after
    the worker drives it to completion
  - `GET /runs/{id}/audit` and `GET /runs/{id}/events` both surface
    `classify_start`, `extract_start`, and a terminal `run_completed`
    event -- CLAUDE.md behavior 1 ("every agent node emits SSE + audit
    events on entry and exit"), and the SSE payload matches the audit
    event by construction (`services.events.emit`)

Deliberately does *not* use the `async_client`/`db_session` fixtures from
conftest.py: those bind route handlers to a SAVEPOINT-nested transaction
that's rolled back at test end and never actually committed to Postgres,
so a separate connection (`run_once()`'s own `AsyncSessionLocal()`) would
never see data created through it. Instead this uses a plain
`httpx.AsyncClient` against the app with its default `get_session`
wiring, so every write is a real commit visible to any other session --
same reasoning as `test_agent_resume.py`, `test_conflicts.py`, and
`test_examine.py`, which all talk to `AsyncSessionLocal` directly rather
than through the SAVEPOINT fixtures. Marked `integration` and skipped
when DATABASE_URL is unreachable, matching that same pattern.
"""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.app.db import AsyncSessionLocal, engine
from backend.app.main import app
from backend.app.models import AuditEvent, Corpus, CostEvent, Document, Run
from backend.app.services import ingestion as ingestion_module
from backend.app.worker import run_once

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

    # Route uploads to a pytest tmp_path instead of the real corpus/ dir on
    # disk, so this test doesn't leave <corpus_id>-named directories behind
    # in the repo.
    from backend.app.config import get_settings

    real_settings = get_settings()
    test_settings = real_settings.model_copy(update={"CORPUS_ROOT": tmp_path})
    monkeypatch.setattr(ingestion_module, "get_settings", lambda: test_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _parse_sse_event_types(body: str) -> list[str]:
    return re.findall(r"^event:\s*(\S+)\s*$", body, flags=re.MULTILINE)


async def test_ingest_then_run_flow(api_client, fake_llm):
    corpus_resp = await api_client.post(
        "/corpora",
        json={"name": "api-runs-test", "inbox_path": "/tmp/api-runs-test-inbox"},
    )
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    doc_resp = await api_client.post(
        f"/corpora/{corpus_id}/documents",
        files={"file": ("prd.md", b"# Feature X\n\nSome PRD content.", "text/markdown")},
    )
    assert doc_resp.status_code == 201
    document = doc_resp.json()
    assert document["corpus_id"] == corpus_id
    assert document["filename"].endswith(".md")

    async with AsyncSessionLocal() as session:
        persisted = await session.get(Document, document["id"])
    assert persisted is not None
    assert len(persisted.chunks) >= 1

    idempotency_key = "api-runs-test-key"
    run_resp_1 = await api_client.post(
        "/runs",
        json={"corpus_id": corpus_id, "kind": "initial"},
        headers={"Idempotency-Key": idempotency_key},
    )
    assert run_resp_1.status_code == 201
    run_id = run_resp_1.json()["id"]
    assert run_resp_1.json()["status"] == "pending"

    # A repeat with the same key returns the same run, not a new one.
    run_resp_2 = await api_client.post(
        "/runs",
        json={"corpus_id": corpus_id, "kind": "initial"},
        headers={"Idempotency-Key": idempotency_key},
    )
    assert run_resp_2.status_code == 201
    assert run_resp_2.json()["id"] == run_id

    # "Restart the worker": claim and drive the pending run to completion.
    claimed_run_id = await run_once()
    assert str(claimed_run_id) == run_id

    detail_resp = await api_client.get(f"/runs/{run_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["status"] == "done"
    assert detail["current_stage"] == "finish"
    assert set(detail["counts"]) == {"claims", "conflicts", "findings"}

    cost_resp = await api_client.get(f"/runs/{run_id}/cost")
    assert cost_resp.status_code == 200
    cost = cost_resp.json()
    assert cost["run_id"] == run_id
    assert cost["total_usd_cost"] == 0.0  # no documents were routed into this run's state yet

    audit_resp = await api_client.get(f"/runs/{run_id}/audit")
    assert audit_resp.status_code == 200
    audit_event_types = [e["event_type"] for e in audit_resp.json()]
    assert "classify_start" in audit_event_types
    assert "extract_start" in audit_event_types
    assert "run_completed" in audit_event_types
    # audit_events are returned in occurred order: classify_start comes
    # before extract_start, which comes before the terminal event.
    assert (
        audit_event_types.index("classify_start")
        < audit_event_types.index("extract_start")
        < audit_event_types.index("run_completed")
    )

    events_resp = await api_client.get(f"/runs/{run_id}/events")
    assert events_resp.status_code == 200
    assert events_resp.headers["content-type"].startswith("text/event-stream")
    sse_event_types = _parse_sse_event_types(events_resp.text)
    assert "classify_start" in sse_event_types
    assert "extract_start" in sse_event_types
    assert "run_completed" in sse_event_types  # the terminal event

    async with AsyncSessionLocal() as session:
        await session.execute(delete(CostEvent).where(CostEvent.run_id == run_id))
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id == run_id))
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(Document).where(Document.corpus_id == corpus_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


async def test_create_run_404s_on_unknown_corpus(api_client):
    resp = await api_client.post(
        "/runs", json={"corpus_id": "00000000-0000-0000-0000-000000000000", "kind": "initial"}
    )
    assert resp.status_code == 404


async def test_upload_document_404s_on_unknown_corpus(api_client):
    resp = await api_client.post(
        "/corpora/00000000-0000-0000-0000-000000000000/documents",
        files={"file": ("prd.md", b"# X\n\ncontent", "text/markdown")},
    )
    assert resp.status_code == 404


async def test_idempotency_key_conflict_across_corpora(api_client):
    corpus_a = (
        await api_client.post(
            "/corpora", json={"name": "idem-a", "inbox_path": "/tmp/idem-a-inbox"}
        )
    ).json()
    corpus_b = (
        await api_client.post(
            "/corpora", json={"name": "idem-b", "inbox_path": "/tmp/idem-b-inbox"}
        )
    ).json()

    key = "cross-corpus-key"
    first = await api_client.post(
        "/runs",
        json={"corpus_id": corpus_a["id"], "kind": "initial"},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 201
    run_id = first.json()["id"]

    conflict = await api_client.post(
        "/runs",
        json={"corpus_id": corpus_b["id"], "kind": "initial"},
        headers={"Idempotency-Key": key},
    )
    assert conflict.status_code == 409

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(Corpus).where(Corpus.id.in_([corpus_a["id"], corpus_b["id"]])))
        await session.commit()
