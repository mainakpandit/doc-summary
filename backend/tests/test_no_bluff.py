"""Test for the `extract` node (implementation plan 8.2), CLAUDE.md
behavior 5 ("no bluff"): every claim's quote must verify verbatim against
its cited chunk's text, or the claim is dropped and a
`claim_rejected_bad_quote` audit event is written instead.

`extract_node` writes `claims`, `claim_sources`, `cost_events` (via
`call_claude`), and `audit_events`, so this is marked `integration` and
skips when DATABASE_URL is unreachable, matching test_classify.py's
pattern. As in test_classify.py, the FakeLLM fixture is seeded by calling
extract.py's own `build_messages` to build the exact message extract_node
will send, rather than hand-computing the cache-key hash.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, select

from backend.app.agent.nodes.extract import SYSTEM_PROMPTS, build_messages, extract_node
from backend.app.agent.state import AgentState, DocumentRef
from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import (
    AuditEvent,
    Chunk,
    Claim,
    ClaimSource,
    Corpus,
    CostEvent,
    Document,
    Finding,
    Run,
)
from backend.tests.fakes import cache_key


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def corpus_run_and_document():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="no-bluff-test", inbox_path="/tmp/no-bluff-test-inbox")
        session.add(corpus)
        await session.flush()
        run = Run(corpus_id=corpus.id, kind="initial", status="running")
        session.add(run)
        document = Document(
            corpus_id=corpus.id,
            filename="prd_checkout.md",
            content_hash=f"hash-{uuid.uuid4()}",
            mime_type="text/markdown",
        )
        session.add(document)
        await session.flush()

        chunk_a = Chunk(
            document_id=document.id,
            idx=0,
            text="The checkout flow will support Apple Pay at launch.",
            char_start=0,
            char_end=53,
        )
        chunk_b = Chunk(
            document_id=document.id,
            idx=1,
            text="Refunds are processed within 3 business days.",
            char_start=53,
            char_end=100,
        )
        session.add_all([chunk_a, chunk_b])
        await session.commit()
        corpus_id, run_id, doc_id = corpus.id, run.id, document.id
        chunk_a_id, chunk_b_id = chunk_a.id, chunk_b.id

    yield corpus_id, run_id, doc_id, chunk_a_id, chunk_b_id

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ClaimSource).where(
                ClaimSource.claim_id.in_(select(Claim.id).where(Claim.run_id == run_id))
            )
        )
        await session.execute(delete(Claim).where(Claim.run_id == run_id))
        await session.execute(delete(Finding).where(Finding.run_id == run_id))
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id == run_id))
        await session.execute(delete(CostEvent).where(CostEvent.run_id == run_id))
        await session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        await session.execute(delete(Document).where(Document.id == doc_id))
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


@pytest.mark.integration
async def test_hallucinated_quote_is_dropped_and_audited(corpus_run_and_document, fake_llm):
    corpus_id, run_id, doc_id, chunk_a_id, chunk_b_id = corpus_run_and_document

    async with AsyncSessionLocal() as session:
        chunks = list(
            (await session.scalars(select(Chunk).where(Chunk.document_id == doc_id))).all()
        )

    valid_claim = {
        "subject": "checkout flow",
        "predicate": "supports",
        "object": "Apple Pay",
        "confidence": 0.95,
        "sources": [
            {
                "chunk_id": str(chunk_a_id),
                "quote": "The checkout flow will support Apple Pay at launch.",
            }
        ],
    }
    hallucinated_claim = {
        "subject": "refunds",
        "predicate": "processed_within",
        "object": "24 hours",
        "confidence": 0.8,
        "sources": [
            {
                # Quote is not an exact substring of any retrieved chunk --
                # the real chunk says "3 business days", not "24 hours".
                "chunk_id": str(chunk_b_id),
                "quote": "Refunds are processed within 24 hours.",
            }
        ],
    }

    messages = build_messages(chunks)
    key = cache_key("extract", SYSTEM_PROMPTS["prd"], messages)
    fake_llm._responses[key] = {
        "text": json.dumps([valid_claim, hallucinated_claim]),
        "input_tokens": 120,
        "output_tokens": 60,
        "stop_reason": "end_turn",
    }

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[DocumentRef(id=doc_id, filename="prd_checkout.md")],
        classifications={doc_id: "prd"},
    )

    result = await extract_node(state)

    assert len(result["claims"]) == 1
    assert result["claims"][0].subject == "checkout flow"
    assert result["claims"][0].sources[0].quote == valid_claim["sources"][0]["quote"]

    async with AsyncSessionLocal() as session:
        persisted_claims = (
            await session.scalars(select(Claim).where(Claim.run_id == run_id))
        ).all()
        assert len(persisted_claims) == 1
        assert persisted_claims[0].subject == "checkout flow"

        persisted_sources = (
            await session.scalars(
                select(ClaimSource).where(ClaimSource.claim_id == persisted_claims[0].id)
            )
        ).all()
        assert len(persisted_sources) == 1

        rejections = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id,
                    AuditEvent.event_type == "claim_rejected_bad_quote",
                )
            )
        ).all()
        assert len(rejections) == 1
        assert rejections[0].payload["subject"] == "refunds"
