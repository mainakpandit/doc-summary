"""Tests for the `build_register` node (implementation plan 8.5, initial run).

As in test_conflicts.py / test_examine.py, claims are produced by running
the real `extract_node` against a `FakeLLM` fixture rather than constructed
by hand, so the `claims`/`claim_sources` rows `build_register_node` reads
back from the database genuinely exist.

Marked `integration` and skipped when DATABASE_URL is unreachable, matching
the other node tests' pattern.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from backend.app.agent.nodes.build_register import build_register_node
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
async def corpus_and_run():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="build-register-test", inbox_path="/tmp/build-register-test-inbox")
        session.add(corpus)
        await session.flush()
        run = Run(corpus_id=corpus.id, kind="initial", status="running")
        session.add(run)
        await session.commit()
        corpus_id, run_id = corpus.id, run.id

    yield corpus_id, run_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id == run_id))
        await session.execute(delete(CostEvent).where(CostEvent.run_id == run_id))
        await session.execute(
            delete(ClaimSource).where(
                ClaimSource.claim_id.in_(select(Claim.id).where(Claim.corpus_id == corpus_id))
            )
        )
        await session.execute(delete(Claim).where(Claim.corpus_id == corpus_id))
        await session.execute(
            delete(Chunk).where(
                Chunk.document_id.in_(select(Document.id).where(Document.corpus_id == corpus_id))
            )
        )
        await session.execute(delete(Document).where(Document.corpus_id == corpus_id))
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


async def _make_document(
    session, corpus_id: uuid.UUID, filename: str, ingested_at=None
) -> Document:
    document = Document(
        corpus_id=corpus_id,
        filename=filename,
        content_hash=f"hash-{uuid.uuid4()}",
        mime_type="text/markdown",
        doc_type="prd",
        **({"ingested_at": ingested_at} if ingested_at is not None else {}),
    )
    session.add(document)
    await session.flush()
    return document


def _claim(subject: str, predicate: str, obj: str, confidence: float, chunk_id, quote: str) -> dict:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "confidence": confidence,
        "sources": [{"chunk_id": str(chunk_id), "quote": quote}],
    }


async def _run_extract(
    state: AgentState, doc_id: uuid.UUID, claims: list[dict], fake_llm
) -> AgentState:
    async with AsyncSessionLocal() as session:
        chunks = list(
            (await session.scalars(select(Chunk).where(Chunk.document_id == doc_id))).all()
        )
    messages = build_messages(chunks)
    key = cache_key("extract", SYSTEM_PROMPTS["prd"], messages)
    fake_llm._responses[key] = {
        "text": json.dumps(claims),
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
    }
    result = await extract_node(state)
    return state.model_copy(update=result)


@pytest.mark.integration
async def test_three_features_produce_one_entry_each_with_sourced_fields(corpus_and_run, fake_llm):
    corpus_id, run_id = corpus_and_run

    chunk_text = (
        "Feature A is owned by Alice, though the team lead is now Alex. "
        "Feature A targets release v2.0 and is in_progress. "
        "Feature A carries a risk of vendor API instability. "
        "Feature B is owned by Bob and targets release v2.1, currently planned. "
        "Feature C is owned by Carol, targets release v3.0, and has shipped. "
        "Feature C carries a risk of data migration delay."
    )

    async with AsyncSessionLocal() as session:
        document = await _make_document(session, corpus_id, "prd_features.md")
        chunk = Chunk(
            document_id=document.id, idx=0, text=chunk_text, char_start=0, char_end=len(chunk_text)
        )
        session.add(chunk)
        await session.commit()
        doc_id, chunk_id = document.id, chunk.id

    claims = [
        # Feature A: two competing owner claims -- higher confidence wins.
        _claim("Feature A", "owner", "Alice", 0.6, chunk_id, "Feature A is owned by Alice"),
        _claim("Feature A", "owner", "Alex", 0.9, chunk_id, "the team lead is now Alex"),
        _claim(
            "Feature A", "target_release", "v2.0", 0.9, chunk_id, "Feature A targets release v2.0"
        ),
        _claim("Feature A", "status", "in_progress", 0.8, chunk_id, "is in_progress"),
        _claim(
            "Feature A",
            "risk",
            "vendor API instability",
            0.7,
            chunk_id,
            "a risk of vendor API instability",
        ),
        # Feature B: no risks at all.
        _claim("Feature B", "owner", "Bob", 0.9, chunk_id, "Feature B is owned by Bob"),
        _claim("Feature B", "target_release", "v2.1", 0.9, chunk_id, "targets release v2.1"),
        _claim("Feature B", "status", "planned", 0.9, chunk_id, "currently planned"),
        # Feature C: duplicate risk objects should collapse to one entry.
        _claim("Feature C", "owner", "Carol", 0.85, chunk_id, "Feature C is owned by Carol"),
        _claim("Feature C", "target_release", "v3.0", 0.85, chunk_id, "targets release v3.0"),
        _claim("Feature C", "status", "shipped", 0.85, chunk_id, "has shipped"),
        _claim(
            "Feature C",
            "risk",
            "data migration delay",
            0.6,
            chunk_id,
            "a risk of data migration delay",
        ),
        _claim(
            "Feature C",
            "risk",
            "data migration delay",
            0.5,
            chunk_id,
            "a risk of data migration delay",
        ),
    ]

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[DocumentRef(id=doc_id, filename="prd_features.md")],
        classifications={doc_id: "prd"},
    )
    state = await _run_extract(state, doc_id, claims, fake_llm)
    assert len(state.claims) == 13

    result = await build_register_node(state)
    additions = result["register_diff"].additions
    assert len(additions) == 3

    by_key = {entry.feature_key: entry.fields for entry in additions}
    assert set(by_key) == {"feature-a", "feature-b", "feature-c"}

    a = by_key["feature-a"]
    assert a["name"] == "Feature A"
    assert a["owner"] == "Alex"  # higher-confidence claim wins over "Alice"
    assert a["target_release"] == "v2.0"
    assert a["status"] == "in_progress"
    assert a["open_risks"] == ["vendor API instability"]

    b = by_key["feature-b"]
    assert b["owner"] == "Bob"
    assert b["target_release"] == "v2.1"
    assert b["status"] == "planned"
    assert b["open_risks"] == []

    c = by_key["feature-c"]
    assert c["owner"] == "Carol"
    assert c["target_release"] == "v3.0"
    assert c["status"] == "shipped"
    assert c["open_risks"] == ["data migration delay"]  # deduplicated

    # Every populated field has at least one citation summary backing it.
    for fields in by_key.values():
        populated_fields = {"name", "owner", "target_release", "status"}
        if fields["open_risks"]:
            populated_fields.add("open_risks")
        cited_fields = {source["field"] for source in fields["sources"]}
        assert populated_fields <= cited_fields
        for source in fields["sources"]:
            assert source["claim_id"]
            assert source["chunk_id"] == str(chunk_id)
            assert source["quote"] in chunk_text

    async with AsyncSessionLocal() as session:
        proposed_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id,
                    AuditEvent.event_type == "register_entry_proposed",
                )
            )
        ).all()
        assert {e.payload["feature_key"] for e in proposed_events} == {
            "feature-a",
            "feature-b",
            "feature-c",
        }


@pytest.mark.integration
async def test_tie_break_uses_most_recent_document_ingested_at(corpus_and_run, fake_llm):
    corpus_id, run_id = corpus_and_run

    old_text = "Feature A targets release v1.0 per the original plan."
    new_text = "Feature A now targets release v1.1 per the revised plan."

    async with AsyncSessionLocal() as session:
        old_doc = await _make_document(
            session, corpus_id, "prd_old.md", ingested_at=datetime(2024, 1, 1, tzinfo=UTC)
        )
        new_doc = await _make_document(
            session, corpus_id, "prd_new.md", ingested_at=datetime(2024, 6, 1, tzinfo=UTC)
        )
        old_chunk = Chunk(
            document_id=old_doc.id, idx=0, text=old_text, char_start=0, char_end=len(old_text)
        )
        new_chunk = Chunk(
            document_id=new_doc.id, idx=0, text=new_text, char_start=0, char_end=len(new_text)
        )
        session.add_all([old_chunk, new_chunk])
        await session.commit()
        old_doc_id, new_doc_id = old_doc.id, new_doc.id
        old_chunk_id, new_chunk_id = old_chunk.id, new_chunk.id

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[
            DocumentRef(id=old_doc_id, filename="prd_old.md"),
            DocumentRef(id=new_doc_id, filename="prd_new.md"),
        ],
        classifications={old_doc_id: "prd", new_doc_id: "prd"},
    )

    old_claim = _claim("Feature A", "target_release", "v1.0", 0.8, old_chunk_id, old_text)
    new_claim = _claim("Feature A", "target_release", "v1.1", 0.8, new_chunk_id, new_text)

    async with AsyncSessionLocal() as session:
        old_chunks = list(
            (await session.scalars(select(Chunk).where(Chunk.document_id == old_doc_id))).all()
        )
        new_chunks = list(
            (await session.scalars(select(Chunk).where(Chunk.document_id == new_doc_id))).all()
        )
    fake_llm._responses[cache_key("extract", SYSTEM_PROMPTS["prd"], build_messages(old_chunks))] = {
        "text": json.dumps([old_claim]),
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
    }
    fake_llm._responses[cache_key("extract", SYSTEM_PROMPTS["prd"], build_messages(new_chunks))] = {
        "text": json.dumps([new_claim]),
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
    }

    extract_result = await extract_node(state)
    state = state.model_copy(update=extract_result)
    assert len(state.claims) == 2

    result = await build_register_node(state)
    additions = result["register_diff"].additions
    assert len(additions) == 1
    # Equal confidence -- the claim sourced from the more recently ingested
    # document wins.
    assert additions[0].fields["target_release"] == "v1.1"


@pytest.mark.integration
async def test_update_run_is_a_noop(corpus_and_run):
    corpus_id, run_id = corpus_and_run

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="update",
        trigger_doc_id=None,
    )

    result = await build_register_node(state)
    assert result == {}

    async with AsyncSessionLocal() as session:
        events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id,
                    AuditEvent.event_type == "build_register_skipped",
                )
            )
        ).all()
        assert len(events) == 1
