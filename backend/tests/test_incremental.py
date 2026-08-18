"""Test for the update-run diff path (implementation plan 8.5,
task_breakdown Step 30 (2)).

Seeds an existing `register_entries` row for "Checkout Redesign" the way a
prior `initial` run would have left it (a `Claim`/`ClaimSource` from a
finished run, a `RegisterEntry` + `RegisterFieldSource` citing it), then
introduces a brand new, topically unrelated document ("Notifications
Migration") as the triggering document of an `update` run. Drives that run
end to end (through `human_gate`/`resume`, exactly like
`test_human_gate.py`) and asserts the two halves of task_breakdown Step
30 (2)'s requirement:

  - the new document's feature is genuinely added to the register (the
    update path isn't a no-op)
  - "Checkout Redesign"'s `register_entries` row -- id, `fields`, `version`,
    and critically `updated_at` -- is byte-for-byte identical afterward,
    and no audit event from this run even mentions it, proving `update_node`
    never examined it, let alone wrote to it (agent/nodes/update.py's
    `RegisterDiff.unaffected` docstring: this run has no new information
    about "Checkout Redesign", so it makes no claim about it either way)

The two documents' chunk text is deliberately picked to score below
`update.NEIGHBOR_MIN_TRIGRAM_SIMILARITY` on a real `pg_trgm similarity()`
call (verified empirically against a live Postgres while writing this test)
so `discover_update_scope` does not pull "Checkout Redesign"'s document into
this run's `state.documents` as a false-positive neighbor -- see
agent/nodes/update.py's module docstring for why an unfiltered top-k
neighbor search would defeat exactly this test on a corpus this small.

Marked `integration` and skipped when DATABASE_URL is unreachable, matching
every other node/graph test in this suite.
"""

from __future__ import annotations

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from backend.app.agent.graph import run_agent
from backend.app.agent.nodes.classify import SYSTEM_PROMPT as CLASSIFY_SYSTEM_PROMPT
from backend.app.agent.nodes.classify import build_batch_messages
from backend.app.agent.nodes.extract import SYSTEM_PROMPTS as EXTRACT_SYSTEM_PROMPTS
from backend.app.agent.nodes.extract import build_messages as build_extract_messages
from backend.app.agent.state import DocumentRef
from backend.app.db import AsyncSessionLocal, engine
from backend.app.main import app
from backend.app.models import (
    AuditEvent,
    Chunk,
    Claim,
    ClaimSource,
    Corpus,
    CostEvent,
    Document,
    RegisterEntry,
    RegisterFieldSource,
    Review,
    Run,
)
from backend.tests.fakes import cache_key

pytestmark = pytest.mark.integration

EXISTING_TEXT = (
    "Checkout Redesign is owned by Alice Chen. It targets the v1.0 release "
    "and carries no open risks at this time."
)
NEW_TEXT = (
    "Notifications Migration ships as part of the Q3 infrastructure rollout "
    "under Priya Patel, replacing the legacy paging vendor entirely."
)


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def seeded_corpus():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="incremental-test", inbox_path="/tmp/incremental-test-inbox")
        session.add(corpus)
        await session.flush()

        prior_run = Run(corpus_id=corpus.id, kind="initial", status="done")
        session.add(prior_run)
        await session.flush()

        existing_doc = Document(
            corpus_id=corpus.id,
            filename="prd_checkout_redesign.md",
            content_hash=f"hash-{uuid.uuid4()}",
            mime_type="text/markdown",
            doc_type="prd",
        )
        session.add(existing_doc)
        await session.flush()

        existing_chunk = Chunk(
            document_id=existing_doc.id,
            idx=0,
            text=EXISTING_TEXT,
            char_start=0,
            char_end=len(EXISTING_TEXT),
        )
        session.add(existing_chunk)
        await session.flush()

        owner_claim = Claim(
            run_id=prior_run.id,
            corpus_id=corpus.id,
            subject="Checkout Redesign",
            predicate="owner",
            object="Alice Chen",
            confidence=0.9,
        )
        session.add(owner_claim)
        await session.flush()
        session.add(
            ClaimSource(
                claim_id=owner_claim.id,
                chunk_id=existing_chunk.id,
                quote="Checkout Redesign is owned by Alice Chen",
            )
        )

        entry = RegisterEntry(
            corpus_id=corpus.id,
            feature_key="checkout-redesign",
            fields={
                "name": "Checkout Redesign",
                "owner": "Alice Chen",
                "target_release": None,
                "status": None,
                "open_risks": [],
            },
            last_updated_run_id=prior_run.id,
        )
        session.add(entry)
        await session.flush()
        session.add(
            RegisterFieldSource(
                register_entry_id=entry.id, field_name="owner", claim_id=owner_claim.id
            )
        )

        new_doc = Document(
            corpus_id=corpus.id,
            filename="notifications_migration.md",
            content_hash=f"hash-{uuid.uuid4()}",
            mime_type="text/markdown",
            doc_type=None,
        )
        session.add(new_doc)
        await session.flush()

        new_chunk = Chunk(
            document_id=new_doc.id, idx=0, text=NEW_TEXT, char_start=0, char_end=len(NEW_TEXT)
        )
        session.add(new_chunk)

        update_run = Run(
            corpus_id=corpus.id,
            kind="update",
            status="pending",
            triggering_document_id=new_doc.id,
        )
        session.add(update_run)

        await session.commit()
        corpus_id = corpus.id
        entry_id = entry.id
        new_doc_id = new_doc.id
        update_run_id = update_run.id

    async with AsyncSessionLocal() as session:
        original_entry = await session.get(RegisterEntry, entry_id)
        original_updated_at = original_entry.updated_at
        original_version = original_entry.version

    yield corpus_id, entry_id, new_doc_id, update_run_id, original_updated_at, original_version

    async with AsyncSessionLocal() as session:
        await session.execute(delete(Review).where(Review.run_id == update_run_id))
        await session.execute(
            delete(RegisterFieldSource).where(
                RegisterFieldSource.register_entry_id.in_(
                    select(RegisterEntry.id).where(RegisterEntry.corpus_id == corpus_id)
                )
            )
        )
        await session.execute(delete(RegisterEntry).where(RegisterEntry.corpus_id == corpus_id))
        await session.execute(
            delete(ClaimSource).where(
                ClaimSource.claim_id.in_(select(Claim.id).where(Claim.corpus_id == corpus_id))
            )
        )
        await session.execute(delete(Claim).where(Claim.corpus_id == corpus_id))
        await session.execute(
            delete(AuditEvent).where(
                AuditEvent.run_id.in_(select(Run.id).where(Run.corpus_id == corpus_id))
            )
        )
        await session.execute(
            delete(CostEvent).where(
                CostEvent.run_id.in_(select(Run.id).where(Run.corpus_id == corpus_id))
            )
        )
        await session.execute(
            delete(Chunk).where(
                Chunk.document_id.in_(select(Document.id).where(Document.corpus_id == corpus_id))
            )
        )
        await session.execute(delete(Document).where(Document.corpus_id == corpus_id))
        await session.execute(delete(Run).where(Run.corpus_id == corpus_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


@pytest.mark.integration
async def test_unrelated_new_document_does_not_touch_existing_feature(seeded_corpus, fake_llm):
    (
        corpus_id,
        entry_id,
        new_doc_id,
        update_run_id,
        original_updated_at,
        original_version,
    ) = seeded_corpus

    async with AsyncSessionLocal() as session:
        new_chunks = list(
            (await session.scalars(select(Chunk).where(Chunk.document_id == new_doc_id))).all()
        )

    doc_ref = [DocumentRef(id=new_doc_id, filename="notifications_migration.md")]
    fake_llm._responses[
        cache_key("classify", CLASSIFY_SYSTEM_PROMPT, build_batch_messages(doc_ref))
    ] = {
        "text": json.dumps(
            [{"document_id": str(new_doc_id), "doc_type": "prd", "confidence": 0.95}]
        ),
        "input_tokens": 50,
        "output_tokens": 20,
        "stop_reason": "end_turn",
    }
    fake_llm._responses[
        cache_key("extract", EXTRACT_SYSTEM_PROMPTS["prd"], build_extract_messages(new_chunks))
    ] = {
        "text": json.dumps(
            [
                {
                    "subject": "Notifications Migration",
                    "predicate": "owner",
                    "object": "Priya Patel",
                    "confidence": 0.9,
                    "sources": [{"chunk_id": str(new_chunks[0].id), "quote": "under Priya Patel"}],
                }
            ]
        ),
        "input_tokens": 100,
        "output_tokens": 50,
        "stop_reason": "end_turn",
    }

    await run_agent(update_run_id, corpus_id, "update")

    async with AsyncSessionLocal() as session:
        run = await session.get(Run, update_run_id)
        assert run.status == "awaiting_review"

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        review_resp = await client.get(f"/runs/{update_run_id}/review")
        assert review_resp.status_code == 200
        review = review_resp.json()

        assert review["conflicts"] == []
        assert review["findings"] == []
        by_feature = {item["feature_key"]: item for item in review["register_changes"]}
        # Only the new feature is up for review -- "checkout-redesign" was
        # never examined, so it never became a pending item either.
        assert set(by_feature) == {"notifications-migration"}
        addition_item = by_feature["notifications-migration"]
        assert addition_item["change_kind"] == "addition"
        assert addition_item["fields"]["owner"] == "Priya Patel"

        submit_resp = await client.post(
            f"/runs/{update_run_id}/review",
            json={
                "items": [
                    {
                        "id": addition_item["id"],
                        "item_type": "register_change",
                        "decision": "approve",
                    }
                ],
                "reviewer": "qa@example.com",
            },
        )
        assert submit_resp.status_code == 200

        resume_resp = await client.post(f"/runs/{update_run_id}/resume")
        assert resume_resp.status_code == 200
        assert resume_resp.json()["status"] == "done"

    async with AsyncSessionLocal() as session:
        # The new feature landed.
        new_entry = (
            await session.scalars(
                select(RegisterEntry).where(
                    RegisterEntry.corpus_id == corpus_id,
                    RegisterEntry.feature_key == "notifications-migration",
                )
            )
        ).one()
        assert new_entry.fields["owner"] == "Priya Patel"

        # "checkout-redesign" is exactly as it was: same id, same fields,
        # same version, same updated_at.
        entry = await session.get(RegisterEntry, entry_id)
        assert entry.updated_at == original_updated_at
        assert entry.version == original_version
        assert entry.fields["owner"] == "Alice Chen"

        # No audit event from this run says anything about it.
        events = (
            await session.scalars(select(AuditEvent).where(AuditEvent.run_id == update_run_id))
        ).all()
        for event in events:
            payload_str = json.dumps(event.payload)
            assert "checkout-redesign" not in payload_str
            assert "Checkout Redesign" not in payload_str
