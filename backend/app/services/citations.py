"""Shared citation-building helpers.

One canonical "citation" shape -- the chunk's own char span plus a padded
text snippet with the quote's highlight offset within that snippet -- used
by both `services/review.py` (the human-gate payload) and
`services/register.py` (the Feature Register GET) so the two agree exactly
on what a claim's source looks like, and the frontend only needs one
`Citation` type for both `SourceButtons`-style UI and the Register page's
popovers.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.chunk import Chunk
from backend.app.models.claim import ClaimSource
from backend.app.models.document import Document

_SNIPPET_PADDING = 80


def build_citation(chunk: Chunk, filename: str, quote: str) -> dict[str, Any]:
    """One citation: the chunk's own char span (already stored, `chunks`
    table) plus a padded text snippet with the quote's offset within that
    snippet -- what review/register payloads use for "text snippet with the
    highlight range"."""
    local_idx = chunk.text.find(quote)
    highlight_len = len(quote) if local_idx != -1 else 0
    local_idx = max(local_idx, 0)
    snippet_start = max(0, local_idx - _SNIPPET_PADDING)
    snippet_end = min(len(chunk.text), local_idx + highlight_len + _SNIPPET_PADDING)
    return {
        "chunk_id": str(chunk.id),
        "document_id": str(chunk.document_id),
        "document_filename": filename,
        "page": chunk.page,
        "quote": quote,
        "char_start": chunk.char_start,
        "char_end": chunk.char_end,
        "snippet": chunk.text[snippet_start:snippet_end],
        "highlight_start": local_idx - snippet_start,
        "highlight_end": local_idx - snippet_start + highlight_len,
    }


async def claim_citations(session: AsyncSession, claim_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(ClaimSource.quote, Chunk, Document.filename)
            .join(Chunk, Chunk.id == ClaimSource.chunk_id)
            .join(Document, Document.id == Chunk.document_id)
            .where(ClaimSource.claim_id == claim_id)
        )
    ).all()
    return [build_citation(chunk, filename, quote) for quote, chunk, filename in rows]
