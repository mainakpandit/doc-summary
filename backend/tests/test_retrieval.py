"""Tests for backend.app.services.retrieval.

Requires no ANTHROPIC_API_KEY or VOYAGE_API_KEY (embed_chunks/embed_query
never construct a real provider once the provider factory is monkeypatched).
Persisting Document/Chunk/CostEvent rows and running vector/trigram queries
needs a live Postgres, so every test here is marked `integration` and skips
(matching test_db_ping.py's pattern) when DATABASE_URL is unreachable.
"""

import pytest
from sqlalchemy import delete, select

from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import Chunk, CostEvent, Document, Run
from backend.app.models import Corpus as CorpusModel
from backend.app.services import embeddings as embeddings_service
from backend.app.services.embeddings import embed_chunks
from backend.app.services.retrieval import retrieve

pytestmark = pytest.mark.integration

DIM = 1536


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
        corpus = CorpusModel(name="retrieval-test", inbox_path="/tmp/retrieval-test-inbox")
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


async def _add_document_with_texts(corpus_id, content_hash: str, texts: list[str]) -> Document:
    async with AsyncSessionLocal() as session:
        document = Document(
            corpus_id=corpus_id,
            filename=f"{content_hash}.txt",
            content_hash=content_hash,
            mime_type="text/plain",
        )
        session.add(document)
        await session.flush()
        offset = 0
        for idx, text in enumerate(texts):
            session.add(
                Chunk(
                    document_id=document.id,
                    idx=idx,
                    text=text,
                    char_start=offset,
                    char_end=offset + len(text),
                )
            )
            offset += len(text)
        await session.commit()
        return document


def _one_hot(index: int, dim: int = DIM) -> list[float]:
    vec = [0.0] * dim
    vec[index] = 1.0
    return vec


def _negated(vector: list[float]) -> list[float]:
    return [-v for v in vector]


class _FixedVectorEmbedder:
    """A stand-in EmbedderProvider that returns a hand-picked vector per
    exact text instead of FakeEmbedder's hash-derived pseudo-random one.

    FakeEmbedder's vectors carry no semantic structure — unrelated text
    embeds to unrelated (effectively random) directions, which is enough to
    exercise embed_chunks/embed_query's plumbing but can't model "a
    paraphrase's embedding lands near its topic's embedding" the way a real
    embedder would. This fake fixes each chunk's and the query's vector by
    exact text lookup, so a test can control *which* text is vector-close
    to the query independently of trigram/keyword overlap.
    """

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]


async def test_retrieve_exact_feature_name_ranks_first(corpus_and_run, fake_embedder):
    corpus_id, _run_id = corpus_and_run
    exact_text = "QuantumSync is the codename for the new offline sync engine shipping in Q3."
    other_a = "The mobile team finished the onboarding redesign this sprint."
    other_b = "Release notes: fixed a crash in the settings screen on Android."
    other_c = "Meeting notes: discussed the hiring plan for the platform team."
    query = "QuantumSync"

    # Chunks are deliberately left unembedded here: FakeEmbedder's vectors
    # are hash-derived pseudo-random noise with no semantic structure (see
    # _FixedVectorEmbedder's docstring below), so for a *small* candidate
    # set its rank contribution can occasionally outweigh a real trigram
    # win by chance, making this assertion flaky. Leaving embedding NULL
    # exercises retrieve()'s empty-vector-list path (it still embeds the
    # query via `fake_embedder`) and isolates what this test is actually
    # about: an exact keyword match wins on the trigram side.
    document = await _add_document_with_texts(
        corpus_id, "exact-match-test-hash", [exact_text, other_a, other_b, other_c]
    )
    chunks = await _fetch_chunks(document.id)
    target = next(c for c in chunks if c.text == exact_text)

    async with AsyncSessionLocal() as session:
        hits = await retrieve(session, corpus_id, query, k=8)

    assert hits, "expected at least one hit"
    assert hits[0].chunk_id == target.id


async def test_retrieve_vector_side_rescues_a_paraphrase(corpus_and_run, monkeypatch):
    corpus_id, run_id = corpus_and_run

    target_text = (
        "Uploads that fail overnight are retried automatically with an "
        "increasing delay between attempts."
    )
    # Contains the query verbatim, so it wins on trigram similarity alone —
    # this is the wrong answer a keyword-only search would surface first.
    decoy_text = (
        "Out of scope for this release: a backoff retry policy for failed "
        "nightly uploads was proposed and rejected in triage."
    )
    filler_a = "The quarterly roadmap review is scheduled for next Tuesday."
    filler_b = "Design system tokens were migrated to the new color palette."
    query = "backoff retry policy for failed nightly uploads"

    target_vector = _one_hot(0)
    filler_vector = _one_hot(1)
    decoy_vector = _negated(target_vector)  # cosine distance 2.0: worst possible match

    fixed_embedder = _FixedVectorEmbedder(
        {
            target_text: target_vector,
            decoy_text: decoy_vector,
            filler_a: filler_vector,
            filler_b: filler_vector,
            query: target_vector,  # query embeds identically to its true (paraphrased) match
        }
    )
    monkeypatch.setattr(embeddings_service, "_provider_factory", lambda: fixed_embedder)

    document = await _add_document_with_texts(
        corpus_id, "paraphrase-test-hash", [target_text, decoy_text, filler_a, filler_b]
    )
    chunks = await _fetch_chunks(document.id)
    target = next(c for c in chunks if c.text == target_text)
    decoy = next(c for c in chunks if c.text == decoy_text)

    async with AsyncSessionLocal() as session:
        await embed_chunks(session, [c.id for c in chunks], run_id)

    async with AsyncSessionLocal() as session:
        hits = await retrieve(session, corpus_id, query, k=8)

    hit_ids = [hit.chunk_id for hit in hits]
    assert hit_ids[0] == target.id, (
        "vector search should rescue the semantically matching paraphrase even "
        "though the decoy contains the query verbatim"
    )
    assert decoy.id in hit_ids, "the keyword decoy should still surface, just not first"
