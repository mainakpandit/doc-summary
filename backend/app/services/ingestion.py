"""Turns a file on disk into a persisted Document plus its Chunks.

Dedupes on (corpus_id, content_hash) before touching the parser, so a file
re-dropped into the inbox is a no-op rather than a re-parse. Chunking runs
independently within each `parsers.Segment` (never across a segment
boundary) as a 1000-char sliding window with 200-char overlap over that
segment's raw text, so every chunk's char_start/char_end stay valid offsets
into the parsed document's full text and every chunk inherits its
segment's page. Embedding is a separate stage (see CLAUDE.md) and does not
happen here.
"""

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models import Chunk, Document
from backend.app.services.parsers import parse_file

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 200


def save_upload(corpus_id: uuid.UUID, filename: str, content: bytes) -> Path:
    """Write an uploaded file's bytes under
    `CORPUS_ROOT/<corpus_id>/<sha256>.<ext>` (task_breakdown Step 24),
    named by content hash rather than the client-supplied filename so
    re-uploading identical bytes overwrites the same path instead of
    accumulating duplicates on disk -- `ingest_file`'s own
    `(corpus_id, content_hash)` dedup then decides whether that content is
    new to this corpus."""
    settings = get_settings()
    content_hash = hashlib.sha256(content).hexdigest()
    ext = Path(filename).suffix.lower()
    corpus_dir = settings.CORPUS_ROOT / str(corpus_id)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    path = corpus_dir / f"{content_hash}{ext}"
    path.write_bytes(content)
    return path


async def get_document_text(document: Document) -> str:
    """Recover a document's full parsed text for `SourceViewer` (the only
    caller: `GET /corpora/{id}/documents/{id}/text`). `ingest_file` never
    persists this whole-document string -- only per-chunk slices, via
    `Chunk.text` -- so the file `save_upload` wrote to disk, named exactly
    `document.filename` per corpus (see its own docstring), is the only
    place it still exists verbatim; re-parsing it is what recovers offsets
    that line up with `Chunk.char_start`/`char_end`."""
    settings = get_settings()
    path = settings.CORPUS_ROOT / str(document.corpus_id) / document.filename
    parsed = await parse_file(path)
    return parsed.text


async def ingest_file(session: AsyncSession, path: Path, corpus_id: uuid.UUID) -> Document:
    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()

    existing = await session.scalar(
        select(Document).where(
            Document.corpus_id == corpus_id, Document.content_hash == content_hash
        )
    )
    if existing is not None:
        return existing

    parsed = await parse_file(path)

    document = Document(
        corpus_id=corpus_id,
        filename=path.name,
        content_hash=content_hash,
        mime_type=parsed.mime_type,
    )
    session.add(document)
    await session.flush()  # assign document.id for the chunks' FK

    idx = 0
    for segment in parsed.segments:
        segment_text = parsed.text[segment.char_start : segment.char_end]
        for window_start, window_end in _sliding_windows(len(segment_text)):
            session.add(
                Chunk(
                    document_id=document.id,
                    idx=idx,
                    text=segment_text[window_start:window_end],
                    page=segment.page,
                    char_start=segment.char_start + window_start,
                    char_end=segment.char_start + window_end,
                )
            )
            idx += 1

    await session.commit()
    return document


def _sliding_windows(
    length: int, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP
) -> Iterator[tuple[int, int]]:
    """Yield (start, end) offsets tiling [0, length) with `size`-char
    windows and `overlap` chars shared between consecutive windows."""
    if length == 0:
        return

    step = size - overlap
    start = 0
    while True:
        end = min(start + size, length)
        yield start, end
        if end == length:
            return
        start += step
