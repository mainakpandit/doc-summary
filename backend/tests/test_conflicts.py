"""Test for the `detect_conflicts` node (implementation plan 8.3).

Seeds `extract_node` (via FakeLLM, see test_no_bluff.py's pattern) with two
claims sharing a `(subject, predicate)` but disagreeing on `object`, each
sourced from a different chunk. `detect_conflicts_node` is then run against
the same `run_id` and must write exactly one `conflicts` row --
`resolution='unresolved'` -- plus one `conflict_detected` audit event, and
both claims' `claim_sources` rows (and therefore both chunks) must be
reachable by following the conflict's `claim_a_id`/`claim_b_id`.

Marked `integration` and skipped when DATABASE_URL is unreachable, matching
test_no_bluff.py's pattern.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, select

from backend.app.agent.nodes.detect_conflicts import detect_conflicts_node
from backend.app.agent.nodes.extract import SYSTEM_PROMPTS, build_messages, extract_node
from backend.app.agent.state import AgentState, DocumentRef
from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import (
    AuditEvent,
    Chunk,
    Claim,
    ClaimSource,
    Conflict,
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
        corpus = Corpus(name="conflicts-test", inbox_path="/tmp/conflicts-test-inbox")
        session.add(corpus)
        await session.flush()
        run = Run(corpus_id=corpus.id, kind="initial", status="running")
        session.add(run)
        document = Document(
            corpus_id=corpus.id,
            filename="prd_launch_date.md",
            content_hash=f"hash-{uuid.uuid4()}",
            mime_type="text/markdown",
        )
        session.add(document)
        await session.flush()

        chunk_a = Chunk(
            document_id=document.id,
            idx=0,
            text="The launch date for Project Nova is set to March 1st.",
            char_start=0,
            char_end=55,
        )
        chunk_b = Chunk(
            document_id=document.id,
            idx=1,
            text="Project Nova will launch on April 15th per the revised plan.",
            char_start=55,
            char_end=117,
        )
        session.add_all([chunk_a, chunk_b])
        await session.commit()
        corpus_id, run_id, doc_id = corpus.id, run.id, document.id
        chunk_a_id, chunk_b_id = chunk_a.id, chunk_b.id

    yield corpus_id, run_id, doc_id, chunk_a_id, chunk_b_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Conflict).where(Conflict.run_id == run_id))
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
async def test_two_claims_with_different_objects_create_one_conflict(
    corpus_run_and_document, fake_llm
):
    corpus_id, run_id, doc_id, chunk_a_id, chunk_b_id = corpus_run_and_document

    async with AsyncSessionLocal() as session:
        chunks = list(
            (await session.scalars(select(Chunk).where(Chunk.document_id == doc_id))).all()
        )

    claim_march = {
        "subject": "Project Nova",
        "predicate": "launch_date",
        "object": "March 1st",
        "confidence": 0.9,
        "sources": [
            {
                "chunk_id": str(chunk_a_id),
                "quote": "The launch date for Project Nova is set to March 1st.",
            }
        ],
    }
    claim_april = {
        "subject": "Project Nova",
        "predicate": "launch_date",
        "object": "April 15th",
        "confidence": 0.9,
        "sources": [
            {
                "chunk_id": str(chunk_b_id),
                "quote": "Project Nova will launch on April 15th per the revised plan.",
            }
        ],
    }

    messages = build_messages(chunks)
    key = cache_key("extract", SYSTEM_PROMPTS["prd"], messages)
    fake_llm._responses[key] = {
        "text": json.dumps([claim_march, claim_april]),
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
    }

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[DocumentRef(id=doc_id, filename="prd_launch_date.md")],
        classifications={doc_id: "prd"},
    )

    extract_result = await extract_node(state)
    assert len(extract_result["claims"]) == 2
    state = state.model_copy(update=extract_result)

    conflicts_result = await detect_conflicts_node(state)

    assert len(conflicts_result["conflicts"]) == 1
    draft = conflicts_result["conflicts"][0]
    assert draft.subject == "Project Nova"
    assert draft.predicate == "launch_date"
    assert draft.resolution == "unresolved"

    async with AsyncSessionLocal() as session:
        persisted_conflicts = (
            await session.scalars(select(Conflict).where(Conflict.run_id == run_id))
        ).all()
        assert len(persisted_conflicts) == 1
        conflict = persisted_conflicts[0]
        assert conflict.resolution == "unresolved"

        claim_ids = {conflict.claim_a_id, conflict.claim_b_id}
        persisted_claims = (
            await session.scalars(select(Claim).where(Claim.id.in_(claim_ids)))
        ).all()
        assert len(persisted_claims) == 2
        assert {c.object for c in persisted_claims} == {"March 1st", "April 15th"}

        sources = (
            await session.scalars(select(ClaimSource).where(ClaimSource.claim_id.in_(claim_ids)))
        ).all()
        assert {s.chunk_id for s in sources} == {chunk_a_id, chunk_b_id}

        audit_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "conflict_detected"
                )
            )
        ).all()
        assert len(audit_events) == 1
        assert audit_events[0].payload["conflict_id"] == str(conflict.id)
        assert set(claim_ids) == {
            uuid.UUID(audit_events[0].payload["claim_a_id"]),
            uuid.UUID(audit_events[0].payload["claim_b_id"]),
        }


@pytest.mark.integration
async def test_no_conflicting_claims_creates_no_conflict(corpus_run_and_document, fake_llm):
    corpus_id, run_id, doc_id, chunk_a_id, _chunk_b_id = corpus_run_and_document

    async with AsyncSessionLocal() as session:
        chunks = list(
            (await session.scalars(select(Chunk).where(Chunk.document_id == doc_id))).all()
        )

    claim = {
        "subject": "Project Nova",
        "predicate": "launch_date",
        "object": "March 1st",
        "confidence": 0.9,
        "sources": [
            {
                "chunk_id": str(chunk_a_id),
                "quote": "The launch date for Project Nova is set to March 1st.",
            }
        ],
    }

    messages = build_messages(chunks)
    key = cache_key("extract", SYSTEM_PROMPTS["prd"], messages)
    fake_llm._responses[key] = {
        "text": json.dumps([claim]),
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
    }

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[DocumentRef(id=doc_id, filename="prd_launch_date.md")],
        classifications={doc_id: "prd"},
    )

    extract_result = await extract_node(state)
    state = state.model_copy(update=extract_result)

    conflicts_result = await detect_conflicts_node(state)
    assert conflicts_result["conflicts"] == []

    async with AsyncSessionLocal() as session:
        persisted_conflicts = (
            await session.scalars(select(Conflict).where(Conflict.run_id == run_id))
        ).all()
        assert persisted_conflicts == []
