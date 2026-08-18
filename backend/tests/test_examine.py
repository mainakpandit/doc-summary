"""Tests for the `examine` node (implementation plan 8.4).

`examine_node` reads `Corpus.rules_path` (here pointed at
`backend/tests/fixtures/rules/examine_rules.yaml`, a fixture mirroring
`corpus/demo/rules.yaml` kept separate so this file doesn't break if the
demo playbook changes independently) and evaluates every rule against
`state.claims`. As in test_conflicts.py, claims are produced by running the
real `extract_node` against a `FakeLLM` fixture rather than constructed by
hand, so `claims`/`claim_sources` rows genuinely exist for `finding_sources`
to reference (its `claim_id` column has a real foreign key).

Marked `integration` and skipped when DATABASE_URL is unreachable, matching
test_conflicts.py's pattern.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from backend.app.agent.nodes.examine import examine_node
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
from backend.app.models.finding import finding_sources
from backend.app.services.rules import (
    PROMPTS_DIR,
    build_llm_messages,
    load_rules,
    select_subjects_for_llm_rule,
)
from backend.tests.fakes import cache_key

RULES_FIXTURE = Path(__file__).parent / "fixtures" / "rules" / "examine_rules.yaml"


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
        corpus = Corpus(
            name="examine-test",
            inbox_path="/tmp/examine-test-inbox",
            rules_path=str(RULES_FIXTURE),
        )
        session.add(corpus)
        await session.flush()
        run = Run(corpus_id=corpus.id, kind="initial", status="running")
        session.add(run)
        document = Document(
            corpus_id=corpus.id,
            filename="prd_features.md",
            content_hash=f"hash-{uuid.uuid4()}",
            mime_type="text/markdown",
            doc_type="prd",
        )
        session.add(document)
        await session.flush()

        chunk = Chunk(
            document_id=document.id,
            idx=0,
            text=(
                "Feature A is owned by Alice and targets release v2.0. "
                "Feature B is owned by Bob and targets release v2.1. "
                "Feature C is owned by Carol, targets release v3.0, and has shipped."
            ),
            char_start=0,
            char_end=200,
        )
        session.add(chunk)
        await session.commit()
        corpus_id, run_id, doc_id, chunk_id = corpus.id, run.id, document.id, chunk.id

    yield corpus_id, run_id, doc_id, chunk_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Finding).where(Finding.run_id == run_id))
        await session.execute(
            delete(ClaimSource).where(
                ClaimSource.claim_id.in_(select(Claim.id).where(Claim.run_id == run_id))
            )
        )
        await session.execute(delete(Claim).where(Claim.run_id == run_id))
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id == run_id))
        await session.execute(delete(CostEvent).where(CostEvent.run_id == run_id))
        await session.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        await session.execute(delete(Document).where(Document.id == doc_id))
        await session.execute(delete(Run).where(Run.id == run_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


async def _run_extract(state: AgentState, chunk_id: uuid.UUID, claims: list[dict], fake_llm):
    async with AsyncSessionLocal() as session:
        chunks = list((await session.scalars(select(Chunk).where(Chunk.id == chunk_id))).all())
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


def _claim(subject: str, predicate: str, obj: str, chunk_id: uuid.UUID, quote: str) -> dict:
    return {
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "confidence": 0.9,
        "sources": [{"chunk_id": str(chunk_id), "quote": quote}],
    }


@pytest.mark.integration
async def test_clean_corpus_produces_no_findings_and_examine_clean_event(
    corpus_run_and_document, fake_llm
):
    corpus_id, run_id, doc_id, chunk_id = corpus_run_and_document

    claims = [
        _claim("Feature A", "owner", "Alice", chunk_id, "Feature A is owned by Alice"),
        _claim("Feature A", "target_release", "v2.0", chunk_id, "targets release v2.0"),
        _claim("Feature B", "owner", "Bob", chunk_id, "Feature B is owned by Bob"),
        _claim("Feature B", "target_release", "v2.1", chunk_id, "targets release v2.1"),
    ]

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[DocumentRef(id=doc_id, filename="prd_features.md")],
        classifications={doc_id: "prd"},
    )
    state = await _run_extract(state, chunk_id, claims, fake_llm)

    examine_result = await examine_node(state)
    assert examine_result["findings"] == []

    async with AsyncSessionLocal() as session:
        persisted_findings = (
            await session.scalars(select(Finding).where(Finding.run_id == run_id))
        ).all()
        assert persisted_findings == []

        clean_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "examine_clean"
                )
            )
        ).all()
        assert len(clean_events) == 1
        assert set(clean_events[0].payload["rules_evaluated"]) == {
            "every_feature_has_owner",
            "every_feature_has_target_release",
            "shipped_requires_release_notes",
        }


@pytest.mark.integration
async def test_missing_owner_produces_one_warning_finding_per_feature(
    corpus_run_and_document, fake_llm
):
    corpus_id, run_id, doc_id, chunk_id = corpus_run_and_document

    claims = [
        _claim("Feature A", "target_release", "v2.0", chunk_id, "targets release v2.0"),
        _claim("Feature B", "target_release", "v2.1", chunk_id, "targets release v2.1"),
    ]

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[DocumentRef(id=doc_id, filename="prd_features.md")],
        classifications={doc_id: "prd"},
    )
    state = await _run_extract(state, chunk_id, claims, fake_llm)

    examine_result = await examine_node(state)
    findings = examine_result["findings"]

    assert len(findings) == 2
    assert {f.subject for f in findings} == {"Feature A", "Feature B"}
    assert all(f.severity == "warning" for f in findings)
    assert all(f.rule_id == "every_feature_has_owner" for f in findings)

    async with AsyncSessionLocal() as session:
        persisted_findings = (
            await session.scalars(select(Finding).where(Finding.run_id == run_id))
        ).all()
        assert len(persisted_findings) == 2
        assert {f.subject for f in persisted_findings} == {"Feature A", "Feature B"}
        assert all(f.severity == "warning" for f in persisted_findings)
        assert all(f.status == "pending" for f in persisted_findings)

        source_rows = (
            await session.execute(
                select(finding_sources).where(
                    finding_sources.c.finding_id.in_([f.id for f in persisted_findings])
                )
            )
        ).all()
        assert len(source_rows) == 2  # one backing claim (target_release) per feature

        clean_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "examine_clean"
                )
            )
        ).all()
        assert clean_events == []


@pytest.mark.integration
async def test_shipped_without_release_notes_produces_error_finding(
    corpus_run_and_document, fake_llm
):
    corpus_id, run_id, doc_id, chunk_id = corpus_run_and_document

    claims = [
        _claim("Feature C", "owner", "Carol", chunk_id, "Feature C is owned by Carol"),
        _claim("Feature C", "target_release", "v3.0", chunk_id, "targets release v3.0"),
        _claim("Feature C", "status", "shipped", chunk_id, "has shipped"),
    ]

    state = AgentState(
        run_id=run_id,
        corpus_id=corpus_id,
        kind="initial",
        trigger_doc_id=None,
        documents=[DocumentRef(id=doc_id, filename="prd_features.md")],
        classifications={doc_id: "prd"},
    )
    state = await _run_extract(state, chunk_id, claims, fake_llm)
    assert len(state.claims) == 3

    rule = next(r for r in load_rules(RULES_FIXTURE) if r.id == "shipped_requires_release_notes")
    selected = select_subjects_for_llm_rule(rule, state.claims)
    doc_types_by_claim = {c.id: ["prd"] for c in state.claims if c.id is not None}
    messages = build_llm_messages(selected, doc_types_by_claim)
    system_prompt = (PROMPTS_DIR / rule.llm.prompt).read_text()

    key = cache_key("examine", system_prompt, messages)
    fake_llm._responses[key] = {
        "text": json.dumps(
            [
                {
                    "subject": "Feature C",
                    "result": False,
                    "reason": "only prd-sourced claims found, no release_notes source",
                }
            ]
        ),
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
    }

    examine_result = await examine_node(state)
    findings = examine_result["findings"]

    assert len(findings) == 1
    assert findings[0].subject == "Feature C"
    assert findings[0].severity == "error"
    assert findings[0].rule_id == "shipped_requires_release_notes"

    async with AsyncSessionLocal() as session:
        persisted_findings = (
            await session.scalars(select(Finding).where(Finding.run_id == run_id))
        ).all()
        assert len(persisted_findings) == 1
        assert persisted_findings[0].severity == "error"

        source_rows = (
            await session.execute(
                select(finding_sources).where(
                    finding_sources.c.finding_id == persisted_findings[0].id
                )
            )
        ).all()
        assert {row.claim_id for row in source_rows} == {c.id for c in state.claims}
