"""Hybrid retrieval over `chunks`: pgvector cosine similarity fused with
pg_trgm keyword search via reciprocal rank fusion (IMPLEMENTATION_PLAN.md
7.6 / TASK_BREAKDOWN.md Step 15). This is the read path other stages (e.g.
the future `extract` node) call to find the passages relevant to a query
within one corpus.

Query embedding reuses `services.embeddings`'s swappable provider seam
(`embed_query`), so `FakeEmbedder` stands in during tests exactly as it
does for `embed_chunks` — no ANTHROPIC_API_KEY or VOYAGE_API_KEY required
(CLAUDE.md behavior 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.chunk import Chunk
from backend.app.models.document import Document
from backend.app.services.embeddings import embed_query

# Per-side candidate pool size before fusion, and the RRF damping constant,
# both fixed by the task spec (Step 15 / plan 7.6).
VECTOR_LIMIT = 40
TRIGRAM_LIMIT = 40
RRF_K = 60

DEFAULT_TOP_K = 8


@dataclass
class ChunkHit:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    text: str
    char_start: int
    char_end: int
    page: int | None
    score: float


async def _vector_search(
    session: AsyncSession, corpus_id: uuid.UUID, query_vector: list[float]
) -> list[Chunk]:
    """Top VECTOR_LIMIT chunks in `corpus_id` by cosine distance
    (`<=>`, matching the `idx_chunks_embedding` hnsw index). Chunks not yet
    embedded (embedding IS NULL) are excluded rather than sorted last."""
    stmt = (
        select(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.corpus_id == corpus_id, Chunk.embedding.is_not(None))
        .order_by(Chunk.embedding.cosine_distance(query_vector))
        .limit(VECTOR_LIMIT)
    )
    return list((await session.scalars(stmt)).all())


async def _trigram_search(session: AsyncSession, corpus_id: uuid.UUID, query: str) -> list[Chunk]:
    """Top TRIGRAM_LIMIT chunks in `corpus_id` by pg_trgm `similarity(text,
    query)`, matching the `idx_chunks_text_trgm` gin index."""
    similarity = func.similarity(Chunk.text, query)
    stmt = (
        select(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.corpus_id == corpus_id)
        .order_by(similarity.desc())
        .limit(TRIGRAM_LIMIT)
    )
    return list((await session.scalars(stmt)).all())


def _reciprocal_rank_fusion(
    ranked_lists: list[list[Chunk]], k: int
) -> dict[uuid.UUID, tuple[Chunk, float]]:
    """Standard RRF: each chunk's fused score is the sum, over every ranked
    list it appears in, of 1 / (k + rank) with rank starting at 1. A chunk
    absent from a list contributes nothing for that list."""
    fused: dict[uuid.UUID, tuple[Chunk, float]] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            _, prior_score = fused.get(chunk.id, (chunk, 0.0))
            fused[chunk.id] = (chunk, prior_score + 1.0 / (k + rank))
    return fused


async def retrieve(
    session: AsyncSession, corpus_id: uuid.UUID, query: str, k: int = DEFAULT_TOP_K
) -> list[ChunkHit]:
    """Embed `query`, run vector and trigram searches over `chunks`
    restricted to documents in `corpus_id`, fuse the two ranked lists via
    reciprocal rank fusion (RRF_K=60), and return the top-`k` hits."""
    query_vector = await embed_query(query)

    vector_hits = await _vector_search(session, corpus_id, query_vector)
    trigram_hits = await _trigram_search(session, corpus_id, query)

    fused = _reciprocal_rank_fusion([vector_hits, trigram_hits], RRF_K)
    ranked = sorted(fused.values(), key=lambda pair: pair[1], reverse=True)[:k]

    return [
        ChunkHit(
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            text=chunk.text,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            page=chunk.page,
            score=score,
        )
        for chunk, score in ranked
    ]
