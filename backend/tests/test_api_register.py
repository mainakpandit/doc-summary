"""Tests for `GET /corpora/{id}/register` (`api/register.py`,
`services/register.py`).

Builds a committed `register_entries` row directly (corpus, document,
chunk, claim, `claim_sources`, `register_entries`, `register_field_sources`)
rather than driving the full agent graph -- same reasoning as
`test_concurrent.py`'s `register_entry_for_lock_test` fixture: this is
purely about what the GET endpoint returns for rows that already exist, not
about how `build_register`/`commit` produce them (that's
`test_build_register.py`'s job).

Uses a plain `httpx.AsyncClient` against the real app (not the
`async_client`/`db_session` SAVEPOINT fixtures) so the fixture's own
`AsyncSessionLocal` writes are visible to the route handler's own session --
same pattern as `test_api_runs.py`. Marked `integration` and skipped when
DATABASE_URL is unreachable.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

from backend.app.db import AsyncSessionLocal, engine
from backend.app.main import app
from backend.app.models import (
    Chunk,
    Claim,
    ClaimSource,
    Corpus,
    Document,
    RegisterEntry,
    RegisterFieldSource,
    Run,
)

pytestmark = pytest.mark.integration


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def register_entry():
    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="register-api-test", inbox_path="/tmp/register-api-test-inbox")
        session.add(corpus)
        await session.flush()

        run = Run(corpus_id=corpus.id, kind="initial", status="done")
        session.add(run)
        await session.flush()

        document = Document(
            corpus_id=corpus.id,
            filename="prd_feature_x.md",
            content_hash=f"hash-{uuid.uuid4()}",
            mime_type="text/markdown",
            doc_type="prd",
        )
        session.add(document)
        await session.flush()

        text = "Feature X is owned by Priya and targets release v3.2."
        chunk = Chunk(document_id=document.id, idx=0, text=text, char_start=0, char_end=len(text))
        session.add(chunk)
        await session.flush()

        claim = Claim(
            run_id=run.id,
            corpus_id=corpus.id,
            subject="Feature X",
            predicate="owner",
            object="Priya",
            confidence=0.9,
        )
        session.add(claim)
        await session.flush()

        session.add(
            ClaimSource(
                claim_id=claim.id,
                chunk_id=chunk.id,
                quote="Feature X is owned by Priya",
            )
        )

        entry = RegisterEntry(
            corpus_id=corpus.id,
            feature_key="feature-x",
            fields={
                "name": "Feature X",
                "owner": "Priya",
                "target_release": "v3.2",
                "status": None,
                "open_risks": [],
            },
        )
        session.add(entry)
        await session.flush()

        session.add(
            RegisterFieldSource(register_entry_id=entry.id, field_name="owner", claim_id=claim.id)
        )
        await session.commit()

        corpus_id, entry_id, claim_id, chunk_id = corpus.id, entry.id, claim.id, chunk.id

    yield corpus_id, entry_id, claim_id, chunk_id

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(RegisterFieldSource).where(RegisterFieldSource.register_entry_id == entry_id)
        )
        await session.execute(delete(RegisterEntry).where(RegisterEntry.id == entry_id))
        await session.execute(delete(ClaimSource).where(ClaimSource.claim_id == claim_id))
        await session.execute(delete(Claim).where(Claim.id == claim_id))
        await session.execute(delete(Chunk).where(Chunk.id == chunk_id))
        await session.execute(delete(Document).where(Document.corpus_id == corpus_id))
        await session.execute(delete(Run).where(Run.corpus_id == corpus_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


@pytest.fixture
async def api_client():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_register_entry_includes_field_claims_and_sources(api_client, register_entry):
    corpus_id, entry_id, claim_id, chunk_id = register_entry

    resp = await api_client.get(f"/corpora/{corpus_id}/register")
    assert resp.status_code == 200
    entries = resp.json()
    assert len(entries) == 1

    entry = entries[0]
    assert entry["id"] == str(entry_id)
    assert entry["feature_key"] == "feature-x"
    assert entry["fields"]["owner"] == "Priya"
    assert entry["fields"]["target_release"] == "v3.2"
    assert entry["version"] == 1

    # Only "owner" has a backing register_field_sources row.
    assert set(entry["field_claims"]) == {"owner"}
    owner_claims = entry["field_claims"]["owner"]
    assert len(owner_claims) == 1
    claim = owner_claims[0]
    assert claim["claim_id"] == str(claim_id)
    assert claim["predicate"] == "owner"
    assert claim["object"] == "Priya"
    # `claims.confidence` is REAL (float4) in Postgres, so 0.9 round-trips
    # as 0.8999999761581421 -- approx, not exact equality.
    assert claim["confidence"] == pytest.approx(0.9)

    sources = claim["sources"]
    assert len(sources) == 1
    source = sources[0]
    assert source["chunk_id"] == str(chunk_id)
    assert source["quote"] == "Feature X is owned by Priya"
    assert source["document_filename"] == "prd_feature_x.md"


async def test_register_empty_for_corpus_with_no_entries(api_client):
    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="register-empty-test", inbox_path="/tmp/register-empty-test-inbox")
        session.add(corpus)
        await session.commit()
        corpus_id = corpus.id

    resp = await api_client.get(f"/corpora/{corpus_id}/register")
    assert resp.status_code == 200
    assert resp.json() == []

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


async def test_register_404s_on_unknown_corpus(api_client):
    resp = await api_client.get(f"/corpora/{uuid.uuid4()}/register")
    assert resp.status_code == 404
