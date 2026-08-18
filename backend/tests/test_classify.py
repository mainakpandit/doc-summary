"""Tests for the `classify` node (implementation plan 8.1).

`classify_node` writes `documents.doc_type`, `cost_events` (via
`call_claude`), and `audit_events`, so these are marked `integration` and
skip when DATABASE_URL is unreachable, matching test_llm_wrapper.py's
pattern.

Rather than hand-computing FakeLLM's cache-key hashes (fragile -- they'd
silently go stale the moment classify.txt's wording changes), `_seed`
below calls classify.py's own `build_batch_messages` to build the exact
messages classify_node will send, then writes the canned response into
`fake_llm`'s response dict under that same key.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import delete, select

from backend.app.agent.nodes.classify import (
    SYSTEM_PROMPT,
    build_batch_messages,
    classify_node,
    classify_review_node,
)
from backend.app.agent.state import AgentState, DocumentRef
from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import AuditEvent, Corpus, CostEvent, Document, Run
from backend.tests.fakes import FakeLLM, cache_key


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
        corpus = Corpus(name="classify-test", inbox_path="/tmp/classify-test-inbox")
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
        await session.execute(delete(Document).where(Document.corpus_id == corpus_id))
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


async def _make_documents(corpus_id: uuid.UUID, filenames: list[str]) -> list[Document]:
    async with AsyncSessionLocal() as session:
        docs = [
            Document(
                corpus_id=corpus_id,
                filename=name,
                content_hash=f"hash-{i}-{uuid.uuid4()}",
                mime_type="text/plain",
            )
            for i, name in enumerate(filenames)
        ]
        session.add_all(docs)
        await session.commit()
        return docs


def _seed(fake_llm: FakeLLM, batch: list[DocumentRef], results: list[dict]) -> None:
    messages = build_batch_messages(batch)
    key = cache_key("classify", SYSTEM_PROMPT, messages)
    fake_llm._responses[key] = {
        "text": json.dumps(results),
        "input_tokens": 50,
        "output_tokens": 20,
        "stop_reason": "end_turn",
    }


@pytest.mark.integration
async def test_classify_confident_batches_update_documents(corpus_and_run, fake_llm):
    corpus_id, run_id = corpus_and_run
    docs = await _make_documents(
        corpus_id,
        ["prd_checkout.md", "techspec_api.md", "standup_notes.txt", "release_v2.md"],
    )
    refs = [DocumentRef(id=d.id, filename=d.filename) for d in docs]

    # 4 documents -> two batches (size 3, size 1) per BATCH_SIZE = 3.
    batch1, batch2 = refs[:3], refs[3:]
    _seed(
        fake_llm,
        batch1,
        [
            {"document_id": str(batch1[0].id), "doc_type": "prd", "confidence": 0.94},
            {"document_id": str(batch1[1].id), "doc_type": "techspec", "confidence": 0.88},
            {"document_id": str(batch1[2].id), "doc_type": "meeting_notes", "confidence": 0.7},
        ],
    )
    _seed(
        fake_llm,
        batch2,
        [{"document_id": str(batch2[0].id), "doc_type": "release_notes", "confidence": 0.99}],
    )

    state = AgentState(
        run_id=run_id, corpus_id=corpus_id, kind="initial", trigger_doc_id=None, documents=refs
    )

    result = await classify_node(state)

    assert result["needs_classification_review"] is False
    assert result["classifications"][docs[0].id] == "prd"
    assert result["classifications"][docs[1].id] == "techspec"
    assert result["classifications"][docs[2].id] == "meeting_notes"
    assert result["classifications"][docs[3].id] == "release_notes"
    assert len(fake_llm.calls) == 2
    assert {call.stage for call in fake_llm.calls} == {"classify"}

    async with AsyncSessionLocal() as session:
        refreshed = await session.get(Document, docs[0].id)
        assert refreshed.doc_type == "prd"

        escalations = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "classify_escalated"
                )
            )
        ).all()
    assert escalations == []


@pytest.mark.integration
async def test_classify_low_confidence_escalates(corpus_and_run, fake_llm):
    corpus_id, run_id = corpus_and_run
    docs = await _make_documents(corpus_id, ["mystery_doc.bin"])
    refs = [DocumentRef(id=docs[0].id, filename=docs[0].filename)]

    _seed(
        fake_llm,
        refs,
        [{"document_id": str(refs[0].id), "doc_type": "other", "confidence": 0.2}],
    )

    state = AgentState(
        run_id=run_id, corpus_id=corpus_id, kind="initial", trigger_doc_id=None, documents=refs
    )

    result = await classify_node(state)

    assert result["needs_classification_review"] is True
    assert result["classifications"][refs[0].id] == "other"

    async with AsyncSessionLocal() as session:
        escalations = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "classify_escalated"
                )
            )
        ).all()

    assert len(escalations) == 1
    assert escalations[0].payload["documents"] == [
        {"document_id": str(refs[0].id), "doc_type": "other", "confidence": 0.2}
    ]


@pytest.mark.integration
async def test_classify_review_stub_records_audit_note(corpus_and_run):
    corpus_id, run_id = corpus_and_run
    state = AgentState(run_id=run_id, corpus_id=corpus_id, kind="initial", trigger_doc_id=None)

    result = await classify_review_node(state)

    assert result == {}
    async with AsyncSessionLocal() as session:
        notes = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "classify_review_noted"
                )
            )
        ).all()
    assert len(notes) == 1
