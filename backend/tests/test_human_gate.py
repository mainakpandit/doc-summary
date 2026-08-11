"""Test for the human gate: `human_gate` node, `commit` node, and
`api/reviews.py` (implementation plan 8.6/8.7, task_breakdown Step 25,
behavior 3).

Drives a run through the real graph (`agent.graph.run_agent`) far enough to
hit the `human_gate` interrupt, fetches the pending items through the real
`GET /runs/{id}/review` API, submits a mixed decision through
`POST /runs/{id}/review` (approve one register addition and the conflict,
reject a sibling register addition and the finding), resumes through
`POST /runs/{id}/resume`, and asserts:

  - the run reaches `awaiting_review` and `GET /review` lists exactly the
    pending conflict, finding, and two register additions, each with
    citations joined back to their chunks
  - after resume the run is `done`, `register_entries` contains only the
    approved feature (the rejected sibling never lands there), and
    `register_field_sources` cite the winning claims
  - the conflict's `resolution` becomes `kept_both` and the finding's
    `status` becomes `rejected`, independent of the register decisions --
    rejecting the finding and the sibling addition does not touch the
    approved conflict or addition

As in test_agent_resume.py / test_conflicts.py / test_build_register.py,
claims/conflicts/findings are seeded directly rather than produced through a
live LLM: `run_agent` always starts a fresh `AgentState` with
`documents=[]` (classify/extract are structural no-ops -- see
docs/assumptions.md), but `detect_conflicts` and `build_register` both read
straight from the database rather than from `state.claims`, so pre-seeded
rows are exactly what they see. Uses a plain `httpx.AsyncClient` against the
real app (not the SAVEPOINT-nested `async_client` fixture) so writes made
inside the graph's own `AsyncSessionLocal()` sessions are visible to the API
calls and vice versa -- same reasoning as test_api_runs.py. Marked
`integration` and skipped when DATABASE_URL is unreachable, matching every
other node test.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, insert, select

from backend.app.agent.graph import run_agent
from backend.app.db import AsyncSessionLocal, engine
from backend.app.main import app
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
    RegisterEntry,
    RegisterFieldSource,
    Review,
    Run,
)
from backend.app.models.finding import finding_sources

pytestmark = pytest.mark.integration

CHUNK_TEXT = (
    "Feature A is owned by Alice. "
    "Feature A targets release 2026-Q1 per the original plan. "
    "A later note says Feature A now targets release 2026-Q2. "
    "Feature B is owned by Bob."
)


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def seeded_run():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="human-gate-test", inbox_path="/tmp/human-gate-test-inbox")
        session.add(corpus)
        await session.flush()

        run = Run(corpus_id=corpus.id, kind="initial", status="pending")
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
            document_id=document.id, idx=0, text=CHUNK_TEXT, char_start=0, char_end=len(CHUNK_TEXT)
        )
        session.add(chunk)
        await session.flush()

        owner_a = Claim(
            run_id=run.id,
            corpus_id=corpus.id,
            subject="Feature A",
            predicate="owner",
            object="Alice",
            confidence=0.9,
        )
        owner_b = Claim(
            run_id=run.id,
            corpus_id=corpus.id,
            subject="Feature B",
            predicate="owner",
            object="Bob",
            confidence=0.9,
        )
        release_1 = Claim(
            run_id=run.id,
            corpus_id=corpus.id,
            subject="Feature A",
            predicate="target_release",
            object="2026-Q1",
            confidence=0.9,
        )
        release_2 = Claim(
            run_id=run.id,
            corpus_id=corpus.id,
            subject="Feature A",
            predicate="target_release",
            object="2026-Q2",
            confidence=0.85,
        )
        session.add_all([owner_a, owner_b, release_1, release_2])
        await session.flush()

        session.add_all(
            [
                ClaimSource(
                    claim_id=owner_a.id, chunk_id=chunk.id, quote="Feature A is owned by Alice"
                ),
                ClaimSource(
                    claim_id=owner_b.id, chunk_id=chunk.id, quote="Feature B is owned by Bob"
                ),
                ClaimSource(
                    claim_id=release_1.id,
                    chunk_id=chunk.id,
                    quote="Feature A targets release 2026-Q1 per the original plan",
                ),
                ClaimSource(
                    claim_id=release_2.id,
                    chunk_id=chunk.id,
                    quote="Feature A now targets release 2026-Q2",
                ),
            ]
        )

        finding = Finding(
            run_id=run.id,
            rule_id="manual_test_rule",
            severity="warning",
            subject="Feature A",
            message="Needs manual review for the human gate test",
            status="pending",
        )
        session.add(finding)
        await session.flush()
        await session.execute(
            insert(finding_sources).values(finding_id=finding.id, claim_id=owner_a.id)
        )

        await session.commit()
        corpus_id, run_id, doc_id = corpus.id, run.id, document.id
        release_1_id = release_1.id

    yield corpus_id, run_id, doc_id, release_1_id

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Review).where(Review.run_id == run_id))
        await session.execute(
            delete(RegisterFieldSource).where(
                RegisterFieldSource.register_entry_id.in_(
                    select(RegisterEntry.id).where(RegisterEntry.corpus_id == corpus_id)
                )
            )
        )
        await session.execute(delete(RegisterEntry).where(RegisterEntry.corpus_id == corpus_id))
        await session.execute(delete(Conflict).where(Conflict.run_id == run_id))
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


@pytest.mark.integration
async def test_mixed_review_decision_survives_resume(seeded_run):
    corpus_id, run_id, _doc_id, release_1_id = seeded_run

    await run_agent(run_id, corpus_id, "initial")

    async with AsyncSessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "awaiting_review"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        review_resp = await client.get(f"/runs/{run_id}/review")
        assert review_resp.status_code == 200
        review = review_resp.json()

        assert len(review["conflicts"]) == 1
        assert len(review["findings"]) == 1
        assert len(review["register_changes"]) == 2

        conflict_item = review["conflicts"][0]
        assert conflict_item["subject"] == "Feature A"
        assert conflict_item["predicate"] == "target_release"
        assert {conflict_item["claim_a"]["object"], conflict_item["claim_b"]["object"]} == {
            "2026-Q1",
            "2026-Q2",
        }
        assert conflict_item["claim_a"]["sources"][0]["quote"] in CHUNK_TEXT

        finding_item = review["findings"][0]
        assert finding_item["rule_id"] == "manual_test_rule"
        assert finding_item["sources"][0]["quote"] == "Feature A is owned by Alice"

        by_feature = {item["feature_key"]: item for item in review["register_changes"]}
        assert set(by_feature) == {"feature-a", "feature-b"}
        feature_a_item = by_feature["feature-a"]
        feature_b_item = by_feature["feature-b"]
        assert feature_a_item["fields"]["owner"] == "Alice"
        assert feature_a_item["fields"]["target_release"] == "2026-Q1"  # higher-confidence claim
        assert any(s["chunk_id"] for s in feature_a_item["sources"])
        assert feature_b_item["fields"]["owner"] == "Bob"

        decisions = {
            "items": [
                {"id": conflict_item["id"], "item_type": "conflict", "decision": "approve"},
                {"id": finding_item["id"], "item_type": "finding", "decision": "reject"},
                {"id": feature_a_item["id"], "item_type": "register_change", "decision": "approve"},
                {
                    "id": feature_b_item["id"],
                    "item_type": "register_change",
                    "decision": "reject",
                    "note": "not ready yet",
                },
            ],
            "reviewer": "qa@example.com",
        }
        submit_resp = await client.post(f"/runs/{run_id}/review", json=decisions)
        assert submit_resp.status_code == 200
        assert submit_resp.json()["accepted"] == 4

        resume_resp = await client.post(f"/runs/{run_id}/resume")
        assert resume_resp.status_code == 200
        assert resume_resp.json()["status"] == "done"

        empty_review_resp = await client.get(f"/runs/{run_id}/review")
        assert empty_review_resp.status_code == 200
        empty_review = empty_review_resp.json()
        assert empty_review["conflicts"] == []
        assert empty_review["findings"] == []
        assert empty_review["register_changes"] == []

    async with AsyncSessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "done"

        conflict = (await session.scalars(select(Conflict).where(Conflict.run_id == run_id))).one()
        assert conflict.resolution == "kept_both"
        assert conflict.resolved_by == "human:qa@example.com"

        finding = (await session.scalars(select(Finding).where(Finding.run_id == run_id))).one()
        assert finding.status == "rejected"
        assert finding.reviewer == "qa@example.com"

        entries = (
            await session.scalars(select(RegisterEntry).where(RegisterEntry.corpus_id == corpus_id))
        ).all()
        assert len(entries) == 1  # the rejected sibling (feature-b) never lands
        entry = entries[0]
        assert entry.feature_key == "feature-a"
        assert entry.fields["owner"] == "Alice"
        assert entry.fields["target_release"] == "2026-Q1"

        field_sources = (
            await session.scalars(
                select(RegisterFieldSource).where(RegisterFieldSource.register_entry_id == entry.id)
            )
        ).all()
        release_sources = [s for s in field_sources if s.field_name == "target_release"]
        assert {s.claim_id for s in release_sources} == {release_1_id}

        committed_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "register_entry_committed"
                )
            )
        ).all()
        assert {e.payload["feature_key"] for e in committed_events} == {"feature-a"}

        rejected_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.run_id == run_id, AuditEvent.event_type == "register_entry_rejected"
                )
            )
        ).all()
        assert {e.payload["feature_key"] for e in rejected_events} == {"feature-b"}

        reviews = (await session.scalars(select(Review).where(Review.run_id == run_id))).all()
        assert len(reviews) == 4
        assert {r.decision for r in reviews} == {"approve", "reject"}
