"""Tests for backend.app.services.ingestion.

Requires no ANTHROPIC_API_KEY. Persisting Document/Chunk rows needs a live
Postgres, so every test here is marked `integration` and skips (matching
test_db_ping.py's pattern) when DATABASE_URL is unreachable.
"""

from itertools import pairwise
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import Chunk, Corpus, Document
from backend.app.services import parsers
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
async def corpus_id():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="ingestion-test", inbox_path="/tmp/ingestion-test-inbox")
        session.add(corpus)
        await session.commit()
        cid = corpus.id

    yield cid

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Document).where(Document.corpus_id == cid))
        await session.execute(delete(Corpus).where(Corpus.id == cid))
        await session.commit()


async def _fetch_chunks(document_id) -> list[Chunk]:
    async with AsyncSessionLocal() as session:
        result = await session.scalars(
            select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.idx)
        )
        return list(result)


async def test_ingest_file_dedupes_same_content_in_same_corpus(corpus_id):
    path = FIXTURES / "sample.txt"

    async with AsyncSessionLocal() as session:
        first = await ingest_file(session, path, corpus_id)

    async with AsyncSessionLocal() as session:
        second = await ingest_file(session, path, corpus_id)

    assert first.id == second.id

    async with AsyncSessionLocal() as session:
        doc_count = await session.scalar(
            select(func.count()).select_from(Document).where(Document.corpus_id == corpus_id)
        )
    assert doc_count == 1

    chunks = await _fetch_chunks(first.id)
    assert chunks, "expected the first (parsing) ingest to have produced chunks"


@pytest.mark.parametrize(
    "filename",
    ["sample.md", "sample.txt", "sample.pdf", "sample.docx", "sample.csv", "sample.json"],
)
async def test_chunks_cover_every_segment_with_no_gaps(corpus_id, filename):
    path = FIXTURES / filename
    parsed = await parsers.parse_file(path)

    async with AsyncSessionLocal() as session:
        document = await ingest_file(session, path, corpus_id)

    chunks = await _fetch_chunks(document.id)
    assert chunks, "expected at least one chunk"

    # Every chunk's stored text must be the verbatim slice of the parsed
    # document's text at its own char_start/char_end.
    for chunk in chunks:
        assert parsed.text[chunk.char_start : chunk.char_end] == chunk.text

    # Within each segment, the chunks derived from it must tile the
    # segment's full span with no gaps (overlap is fine, a gap is not).
    for segment in parsed.segments:
        segment_chunks = [
            c for c in chunks if segment.char_start <= c.char_start < segment.char_end
        ]

        if segment.char_start == segment.char_end:
            assert segment_chunks == []
            continue

        assert segment_chunks, f"no chunks produced for segment {segment}"
        assert segment_chunks[0].char_start == segment.char_start
        assert segment_chunks[-1].char_end == segment.char_end
        for prev, nxt in pairwise(segment_chunks):
            assert nxt.char_start > prev.char_start, "windows must advance"
            assert nxt.char_start <= prev.char_end, "gap between consecutive chunks"
        assert [c.page for c in segment_chunks] == [segment.page] * len(segment_chunks)


async def test_large_multipage_pdf_propagates_page_numbers_onto_every_chunk(corpus_id):
    path = FIXTURES / "sample_large.pdf"
    parsed = await parsers.parse_file(path)
    assert [s.page for s in parsed.segments] == [1, 2, 3]

    async with AsyncSessionLocal() as session:
        document = await ingest_file(session, path, corpus_id)

    chunks = await _fetch_chunks(document.id)

    # Page 2's segment is long enough to require more than one sliding
    # window, so this is the case that actually exercises page
    # propagation across multiple chunks of the same page.
    page_two_chunks = [c for c in chunks if c.page == 2]
    assert len(page_two_chunks) > 1

    for segment in parsed.segments:
        segment_chunks = [c for c in chunks if c.page == segment.page]
        assert segment_chunks, f"no chunks for page {segment.page}"
        assert segment_chunks[0].char_start == segment.char_start
        assert segment_chunks[-1].char_end == segment.char_end
        for c in segment_chunks:
            assert segment.char_start <= c.char_start <= c.char_end <= segment.char_end
