"""Tests for backend.app.services.embeddings, exercised through
FakeEmbedder (and, for retry behavior, a small hand-rolled fake provider
that raises httpx network errors on demand).

Requires no ANTHROPIC_API_KEY or VOYAGE_API_KEY — FakeEmbedder never
constructs a real VoyageProvider. Persisting Document/Chunk/CostEvent rows
needs a live Postgres, so every test here is marked `integration` and
skips (matching test_db_ping.py's pattern) when DATABASE_URL is
unreachable.
"""

from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, select

from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import Chunk, CostEvent, Document, Run
from backend.app.models import Corpus as CorpusModel
from backend.app.services import embeddings as embeddings_service
from backend.app.services.embeddings import BATCH_SIZE, MAX_RETRIES, embed_chunks
from backend.app.services.ingestion import ingest_file

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures" / "parsers"


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def corpus_and_run():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = CorpusModel(name="embeddings-test", inbox_path="/tmp/embeddings-test-inbox")
        session.add(corpus)
        await session.flush()
        run = Run(corpus_id=corpus.id, kind="initial", status="running")
        session.add(run)
        await session.commit()
        cid, rid = corpus.id, run.id

    yield cid, rid

    async with AsyncSessionLocal() as session:
        await session.execute(delete(CostEvent).where(CostEvent.run_id == rid))
        await session.execute(delete(Document).where(Document.corpus_id == cid))
        await session.execute(delete(Run).where(Run.id == rid))
        await session.execute(delete(CorpusModel).where(CorpusModel.id == cid))
        await session.commit()


async def _fetch_chunks(document_id) -> list[Chunk]:
    async with AsyncSessionLocal() as session:
        result = await session.scalars(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.idx)
        )
        return list(result)


async def _add_document_with_chunks(corpus_id, content_hash: str, chunk_count: int) -> Document:
    async with AsyncSessionLocal() as session:
        document = Document(
            corpus_id=corpus_id,
            filename=f"{content_hash}.txt",
            content_hash=content_hash,
            mime_type="text/plain",
        )
        session.add(document)
        await session.flush()
        for idx in range(chunk_count):
            session.add(
                Chunk(
                    document_id=document.id,
                    idx=idx,
                    text=f"chunk body number {idx}",
                    char_start=idx * 10,
                    char_end=idx * 10 + 10,
                )
            )
        await session.commit()
        return document


class _FlakyProvider:
    """Fails with a network error on the first `fail_times` calls, then
    succeeds, so retry/backoff behavior can be exercised deterministically
    without a real network."""

    def __init__(self, fail_times: int, dim: int = 1536) -> None:
        self.fail_times = fail_times
        self.dim = dim
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("simulated network failure")
        return [[0.1] * self.dim for _ in texts]


@pytest.fixture
def no_sleep(monkeypatch):
    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(embeddings_service.asyncio, "sleep", _instant)


async def test_embed_chunks_populates_embedding_column(corpus_and_run, fake_embedder):
    corpus_id, run_id = corpus_and_run

    async with AsyncSessionLocal() as session:
        document = await ingest_file(session, FIXTURES / "sample.txt", corpus_id)

    chunks = await _fetch_chunks(document.id)
    assert chunks, "expected ingestion to have produced chunks"
    assert all(chunk.embedding is None for chunk in chunks)

    async with AsyncSessionLocal() as session:
        await embed_chunks(session, [chunk.id for chunk in chunks], run_id)

    embedded = await _fetch_chunks(document.id)
    assert len(embedded) == len(chunks)
    for chunk in embedded:
        assert chunk.embedding is not None
        assert len(chunk.embedding) == fake_embedder.dim


async def test_embed_chunks_batches_provider_calls_by_96(corpus_and_run, fake_embedder):
    corpus_id, _run_id = corpus_and_run
    chunk_count = BATCH_SIZE * 2 + 5
    document = await _add_document_with_chunks(corpus_id, "batch-test-hash", chunk_count)
    chunks = await _fetch_chunks(document.id)

    async with AsyncSessionLocal() as session:
        await embed_chunks(session, [chunk.id for chunk in chunks], _run_id)

    assert [len(call) for call in fake_embedder.calls] == [BATCH_SIZE, BATCH_SIZE, 5]

    embedded = await _fetch_chunks(document.id)
    assert all(chunk.embedding is not None for chunk in embedded)


async def test_embed_chunks_writes_cost_event_for_embed_stage(corpus_and_run, fake_embedder):
    corpus_id, run_id = corpus_and_run
    document = await _add_document_with_chunks(corpus_id, "cost-event-test-hash", 3)
    chunks = await _fetch_chunks(document.id)

    async with AsyncSessionLocal() as session:
        await embed_chunks(session, [chunk.id for chunk in chunks], run_id)

    async with AsyncSessionLocal() as session:
        events = (await session.scalars(select(CostEvent).where(CostEvent.run_id == run_id))).all()

    assert len(events) == 1
    event = events[0]
    assert event.stage == "embed"
    assert event.model == embeddings_service.EMBEDDING_MODEL
    assert event.input_tokens > 0
    assert event.output_tokens == 0
    assert event.usd_cost > 0


async def test_embed_chunks_retries_transient_network_errors_then_succeeds(
    corpus_and_run, monkeypatch, no_sleep
):
    corpus_id, run_id = corpus_and_run
    document = await _add_document_with_chunks(corpus_id, "retry-success-test-hash", 2)
    chunks = await _fetch_chunks(document.id)

    provider = _FlakyProvider(fail_times=MAX_RETRIES)
    monkeypatch.setattr(embeddings_service, "_provider_factory", lambda: provider)

    async with AsyncSessionLocal() as session:
        await embed_chunks(session, [chunk.id for chunk in chunks], run_id)

    assert provider.calls == MAX_RETRIES + 1

    embedded = await _fetch_chunks(document.id)
    assert all(chunk.embedding is not None for chunk in embedded)

    async with AsyncSessionLocal() as session:
        events = (await session.scalars(select(CostEvent).where(CostEvent.run_id == run_id))).all()
    assert len(events) == 1


async def test_embed_chunks_raises_and_writes_nothing_after_exhausting_retries(
    corpus_and_run, monkeypatch, no_sleep
):
    corpus_id, run_id = corpus_and_run
    document = await _add_document_with_chunks(corpus_id, "retry-exhausted-test-hash", 2)
    chunks = await _fetch_chunks(document.id)

    provider = _FlakyProvider(fail_times=MAX_RETRIES + 1)
    monkeypatch.setattr(embeddings_service, "_provider_factory", lambda: provider)

    async with AsyncSessionLocal() as session:
        with pytest.raises(httpx.ConnectError):
            await embed_chunks(session, [chunk.id for chunk in chunks], run_id)

    assert provider.calls == MAX_RETRIES + 1

    embedded = await _fetch_chunks(document.id)
    assert all(chunk.embedding is None for chunk in embedded)

    async with AsyncSessionLocal() as session:
        events = (await session.scalars(select(CostEvent).where(CostEvent.run_id == run_id))).all()
    assert events == []
