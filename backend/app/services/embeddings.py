"""Provider seam plus batched chunk embedding (see CLAUDE.md: every source
chunk gets a vector; embedding cost is tracked like every other model call).

Mirrors services/llm.py's swappable-factory pattern: `_provider_factory` is
a module-level attribute so tests can monkeypatch it to `FakeEmbedder`
(backend/tests/fakes.py, via the `fake_embedder` fixture in
backend/tests/conftest.py) without touching call sites or requiring a real
embedding API key.

`embed_chunks` loads the requested chunks, sends their text to the
provider in batches of `BATCH_SIZE`, retries a batch up to `MAX_RETRIES`
times with exponential backoff when the provider raises a network error,
then writes every resulting vector back in a single bulk `UPDATE` and
records one `cost_events` row for the "embed" stage. Nothing is written
until every batch has succeeded, so a failed call never partially bills or
partially embeds (mirrors CLAUDE.md behavior 2: a killed/retried call
never re-bills work that already committed, because nothing here commits
until all of it succeeds).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Protocol

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models.chunk import Chunk
from backend.app.models.cost import CostEvent

logger = structlog.get_logger(__name__)

# Voyage AI's `voyage-3` general-purpose embedding model, called directly
# over its REST API (no extra SDK dependency). Chosen over a native
# Anthropic embedding endpoint because Anthropic does not offer one; Voyage
# is Anthropic's recommended embedding partner. Matches the EMBEDDING_MODEL
# / EMBEDDING_DIM defaults already fixed in config.py and the
# chunks.embedding `Vector(1536)` column (see docs/assumptions.md for the
# full rationale, including the dimension caveat).
EMBEDDING_MODEL = "voyage-3"
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"

# USD per 1M input tokens for EMBEDDING_MODEL. Source:
# https://docs.voyageai.com/docs/pricing. Checked 2026-08-10 — re-check this
# constant whenever EMBEDDING_MODEL changes.
EMBEDDING_PRICE_PER_MILLION_TOKENS = Decimal("0.06")

BATCH_SIZE = 96
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0

_MILLION = Decimal(1_000_000)


class EmbedderProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VoyageProvider:
    """Talks to Voyage AI's embeddings REST API directly over httpx.
    Requests use input_type="document" — this class is for ingestion-time
    chunk embedding only; embedding a user query for retrieval.py is a
    separate call site that should pass input_type="query" instead."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.VOYAGE_API_KEY
        self._client = httpx.AsyncClient(timeout=30.0)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.post(
            VOYAGE_API_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={"model": EMBEDDING_MODEL, "input": texts, "input_type": "document"},
        )
        response.raise_for_status()
        payload = response.json()
        ordered = sorted(payload["data"], key=lambda item: item["index"])
        return [item["embedding"] for item in ordered]


def _default_provider() -> EmbedderProvider:
    return VoyageProvider()


# Swappable factory — backend/tests/conftest.py's fake_embedder fixture
# monkeypatches this attribute (not any call site) so FakeEmbedder stands
# in for the real provider.
_provider_factory: Callable[[], EmbedderProvider] = _default_provider


def _batches(items: list[Chunk], size: int) -> Iterator[list[Chunk]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _estimate_tokens(text: str) -> int:
    """~4 chars/token, mirroring services/llm.py's pre-call estimate.
    EmbedderProvider.embed only returns vectors (no usage block), so this
    is the only token signal available for costing the "embed" stage."""
    return max(len(text) // 4, 1)


def _usd_cost(total_tokens: int) -> Decimal:
    return Decimal(total_tokens) / _MILLION * EMBEDDING_PRICE_PER_MILLION_TOKENS


async def _embed_batch_with_retry(
    provider: EmbedderProvider, texts: list[str]
) -> list[list[float]]:
    """Retry a single batch up to MAX_RETRIES times, doubling the delay
    each time, but only for network errors (httpx.TransportError) — an
    HTTP error response (bad request, auth failure) is not transient and
    is raised immediately instead of being retried."""
    attempt = 0
    while True:
        try:
            return await provider.embed(texts)
        except httpx.TransportError:
            if attempt >= MAX_RETRIES:
                raise
            delay = BACKOFF_BASE_SECONDS * (2**attempt)
            logger.warning("embed_batch_retry", attempt=attempt + 1, delay_seconds=delay)
            await asyncio.sleep(delay)
            attempt += 1


async def embed_chunks(
    session: AsyncSession, chunk_ids: list[uuid.UUID], run_id: uuid.UUID
) -> None:
    """Load `chunk_ids`, embed their text in batches of BATCH_SIZE, and
    persist the result. `run_id` attributes the "embed" cost_events row to
    a run (cost_events.run_id is NOT NULL — see docs/assumptions.md for why
    this parameter isn't in the task_breakdown.md signature verbatim)."""
    if not chunk_ids:
        return

    chunks = list((await session.scalars(select(Chunk).where(Chunk.id.in_(chunk_ids)))).all())
    if not chunks:
        return

    provider = _provider_factory()

    vectors_by_id: dict[uuid.UUID, list[float]] = {}
    total_tokens = 0
    start = time.monotonic()
    for batch in _batches(chunks, BATCH_SIZE):
        texts = [chunk.text for chunk in batch]
        vectors = await _embed_batch_with_retry(provider, texts)
        for chunk, vector in zip(batch, vectors, strict=True):
            vectors_by_id[chunk.id] = vector
        total_tokens += sum(_estimate_tokens(text) for text in texts)
    latency_ms = int((time.monotonic() - start) * 1000)

    usd_cost = _usd_cost(total_tokens)

    # "ORM Bulk UPDATE by Primary Key": a bare `update(Chunk)` executed with
    # a list of {"id": ..., "embedding": ...} dicts is the single statement
    # SQLAlchemy 2.0 compiles into one executemany UPDATE keyed by primary
    # key — the param dict keys must match the mapped attribute names for
    # this to be recognized.
    await session.execute(
        update(Chunk),
        [{"id": chunk_id, "embedding": vector} for chunk_id, vector in vectors_by_id.items()],
    )
    session.add(
        CostEvent(
            run_id=run_id,
            stage="embed",
            model=EMBEDDING_MODEL,
            input_tokens=total_tokens,
            output_tokens=0,
            latency_ms=latency_ms,
            usd_cost=usd_cost,
        )
    )
    await session.commit()

    logger.info(
        "embed_chunks",
        run_id=str(run_id),
        chunk_count=len(vectors_by_id),
        input_tokens=total_tokens,
        usd_cost=str(usd_cost),
        latency_ms=latency_ms,
    )


async def embed_query(text: str) -> list[float]:
    """Embed a single query string through the same swappable provider seam
    as embed_chunks, for services/retrieval.py's vector search. Untimed and
    uncosted (no cost_events row): a query embedding is orders of magnitude
    smaller than a batch of document chunks, and retrieval has no run_id of
    its own to attribute a cost row to."""
    provider = _provider_factory()
    [vector] = await provider.embed([text])
    return vector
