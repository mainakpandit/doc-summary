"""Tests for `GET /corpora/{corpus_id}/documents/{document_id}/text`.

Added to serve `SourceViewer` (frontend, task_breakdown Step 30's review
gate): the endpoint list in IMPLEMENTATION_PLAN.md section 9 never
included a way to fetch a document's full text, but chunk `char_start`/
`char_end` are offsets into exactly that text and highlighting a citation
needs it. See docs/assumptions.md.

Follows test_api_runs.py's pattern: a plain `httpx.AsyncClient` against
the real app (not the SAVEPOINT-nested `async_client` fixture from
conftest.py), `CORPUS_ROOT` monkeypatched to `tmp_path` so uploads don't
land in the repo's real `corpus/` dir, and `pytest.mark.integration` with
a reachability skip since it needs a live Postgres.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.app.db import AsyncSessionLocal, engine
from backend.app.main import app
from backend.app.models import Corpus, Document
from backend.app.services import ingestion as ingestion_module

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

    real_settings = get_settings()
    test_settings = real_settings.model_copy(update={"CORPUS_ROOT": tmp_path})
    monkeypatch.setattr(ingestion_module, "get_settings", lambda: test_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_get_document_text_returns_full_parsed_text(api_client):
    corpus_resp = await api_client.post(
        "/corpora", json={"name": "doc-text-test", "inbox_path": "/tmp/doc-text-test-inbox"}
    )
    assert corpus_resp.status_code == 201
    corpus_id = corpus_resp.json()["id"]

    content = b"# Feature X\n\nSome PRD content about Feature X."
    doc_resp = await api_client.post(
        f"/corpora/{corpus_id}/documents",
        files={"file": ("prd.md", content, "text/markdown")},
    )
    assert doc_resp.status_code == 201
    document_id = doc_resp.json()["id"]

    text_resp = await api_client.get(f"/corpora/{corpus_id}/documents/{document_id}/text")
    assert text_resp.status_code == 200
    body = text_resp.json()
    assert body["document_id"] == document_id
    assert "Feature X" in body["text"]

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Document).where(Document.corpus_id == corpus_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


async def test_get_document_text_404s_on_mismatched_corpus(api_client):
    corpus_a = (
        await api_client.post(
            "/corpora", json={"name": "doc-text-a", "inbox_path": "/tmp/doc-text-a-inbox"}
        )
    ).json()
    corpus_b = (
        await api_client.post(
            "/corpora", json={"name": "doc-text-b", "inbox_path": "/tmp/doc-text-b-inbox"}
        )
    ).json()

    doc_resp = await api_client.post(
        f"/corpora/{corpus_a['id']}/documents",
        files={"file": ("prd.md", b"# X\n\ncontent", "text/markdown")},
    )
    document_id = doc_resp.json()["id"]

    resp = await api_client.get(f"/corpora/{corpus_b['id']}/documents/{document_id}/text")
    assert resp.status_code == 404

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(Document).where(Document.corpus_id.in_([corpus_a["id"], corpus_b["id"]]))
        )
        await session.execute(delete(Corpus).where(Corpus.id.in_([corpus_a["id"], corpus_b["id"]])))
        await session.commit()


async def test_get_document_text_404s_on_unknown_document(api_client):
    corpus_resp = await api_client.post(
        "/corpora", json={"name": "doc-text-missing", "inbox_path": "/tmp/doc-text-missing-inbox"}
    )
    corpus_id = corpus_resp.json()["id"]

    resp = await api_client.get(
        f"/corpora/{corpus_id}/documents/00000000-0000-0000-0000-000000000000/text"
    )
    assert resp.status_code == 404

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()
