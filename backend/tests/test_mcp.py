"""Test for the MCP server (implementation plan section 11, task_breakdown
Step 30 (3), CLAUDE.md behavior 4: "Machine drivable").

Spawns `python -m backend.app.mcp_server` as a real subprocess (stdio
transport) with this test's own `DATABASE_URL` (the session's testcontainers
Postgres, set into `os.environ` by `conftest.py`'s `pytest_configure`)
forwarded into its environment, so the subprocess's `AsyncSessionLocal`
reaches the exact same database this test and the in-process worker write
to. Drives a full flow through MCP tools alone -- `create_corpus`,
`add_document`, `start_run`, then (after the in-process worker claims and
runs it, the same way `test_api_runs.py`/`test_human_gate.py` do)
`list_review_items`, `submit_review`, `resume_run`, `get_register` -- and
asserts the resulting register via `get_register` matches the same corpus's
`GET /corpora/{id}/register` over plain HTTP.

`mcp` (2.0.0)'s `CallToolResult.structured_content` carries a tool's typed
return value directly for a JSON-object return type (every tool here except
the list-returning ones); a JSON-array return type is wrapped as
`{"result": [...]}` (protocol requirement: structured content must be a
JSON object) -- `_unwrap` below normalizes both, verified empirically by
spawning the real server before writing this test.

Marked `integration` and skipped when DATABASE_URL is unreachable, matching
every other integration test in this suite.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from sqlalchemy import delete, select

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
from backend.app.services import llm as llm_service
from backend.app.worker import run_once
from backend.tests.fakes import FakeLLM, cache_key

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

PRD_TEXT = "Feature Z is owned by Dana Kim and targets release v3.0. No open risks reported."


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


def _unwrap(structured: Any) -> Any:
    """Undo the JSON-array-return `{"result": [...]}` wrapping described in
    this module's docstring; a JSON-object return passes through as-is."""
    if isinstance(structured, dict) and structured.keys() == {"result"}:
        return structured["result"]
    return structured


@pytest.mark.integration
async def test_full_flow_via_mcp_matches_http_register(tmp_path):
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    prd_path = tmp_path / "prd_feature_z.md"
    prd_path.write_text(f"# Feature Z\n\n{PRD_TEXT}\n")

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "backend.app.mcp_server"],
        env=dict(os.environ),
        cwd=str(REPO_ROOT),
    )

    corpus_id: str | None = None
    run_id: str | None = None
    mcp_register: Any = None

    try:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {
                "list_corpora",
                "create_corpus",
                "add_document",
                "start_run",
                "get_run",
                "list_review_items",
                "submit_review",
                "resume_run",
                "get_register",
                "get_cost",
                "get_audit",
            }

            corpus_result = await session.call_tool(
                "create_corpus",
                {"name": "mcp-test", "inbox_path": str(tmp_path / "inbox")},
            )
            assert not corpus_result.is_error
            corpus = _unwrap(corpus_result.structured_content)
            corpus_id = corpus["id"]

            doc_result = await session.call_tool(
                "add_document", {"corpus_id": corpus_id, "file_path": str(prd_path)}
            )
            assert not doc_result.is_error
            document = _unwrap(doc_result.structured_content)
            document_id = document["id"]

            run_result = await session.call_tool(
                "start_run", {"corpus_id": corpus_id, "kind": "initial"}
            )
            assert not run_result.is_error
            run = _unwrap(run_result.structured_content)
            run_id = run["id"]
            assert run["status"] == "pending"

            # Drive the graph in-process with FakeLLM, exactly like
            # test_api_runs.py/test_human_gate.py -- the MCP server only
            # enqueues runs, same as POST /runs; a worker process runs
            # them, and this test stands in for that worker.
            async with AsyncSessionLocal() as db_session:
                persisted = await db_session.get(Document, uuid.UUID(document_id))
                chunks = list(persisted.chunks)

            fake_llm = FakeLLM()
            doc_ref = [DocumentRef(id=uuid.UUID(document_id), filename=persisted.filename)]
            fake_llm._responses[
                cache_key("classify", CLASSIFY_SYSTEM_PROMPT, build_batch_messages(doc_ref))
            ] = {
                "text": json.dumps(
                    [{"document_id": document_id, "doc_type": "prd", "confidence": 0.95}]
                ),
                "input_tokens": 50,
                "output_tokens": 20,
                "stop_reason": "end_turn",
            }
            fake_llm._responses[
                cache_key("extract", EXTRACT_SYSTEM_PROMPTS["prd"], build_extract_messages(chunks))
            ] = {
                "text": json.dumps(
                    [
                        {
                            "subject": "Feature Z",
                            "predicate": "owner",
                            "object": "Dana Kim",
                            "confidence": 0.9,
                            "sources": [
                                {"chunk_id": str(chunks[0].id), "quote": "owned by Dana Kim"}
                            ],
                        },
                        {
                            "subject": "Feature Z",
                            "predicate": "target_release",
                            "object": "v3.0",
                            "confidence": 0.9,
                            "sources": [
                                {
                                    "chunk_id": str(chunks[0].id),
                                    "quote": "targets release v3.0",
                                }
                            ],
                        },
                    ]
                ),
                "input_tokens": 100,
                "output_tokens": 60,
                "stop_reason": "end_turn",
            }
            llm_service._provider_factory = lambda: fake_llm
            try:
                claimed = await run_once()
            finally:
                llm_service._provider_factory = llm_service._default_provider
            assert str(claimed) == run_id

            status_result = await session.call_tool("get_run", {"run_id": run_id})
            run_status = _unwrap(status_result.structured_content)
            assert run_status["status"] == "awaiting_review"

            review_result = await session.call_tool("list_review_items", {"run_id": run_id})
            review = _unwrap(review_result.structured_content)
            assert review["conflicts"] == []
            assert review["findings"] == []
            assert len(review["register_changes"]) == 1
            addition = review["register_changes"][0]
            assert addition["feature_key"] == "feature-z"
            assert addition["fields"]["owner"] == "Dana Kim"

            submit_result = await session.call_tool(
                "submit_review",
                {
                    "run_id": run_id,
                    "decisions": [
                        {
                            "id": addition["id"],
                            "item_type": "register_change",
                            "decision": "approve",
                        }
                    ],
                    "reviewer": "mcp-test@example.com",
                },
            )
            assert _unwrap(submit_result.structured_content)["accepted"] == 1

            resume_result = await session.call_tool("resume_run", {"run_id": run_id})
            resumed_run = _unwrap(resume_result.structured_content)
            assert resumed_run["status"] == "done"

            mcp_register_result = await session.call_tool("get_register", {"corpus_id": corpus_id})
            mcp_register = _unwrap(mcp_register_result.structured_content)

            cost_result = await session.call_tool("get_cost", {"run_id": run_id})
            cost = _unwrap(cost_result.structured_content)
            assert cost["total_usd_cost"] > 0.0
            assert {s["stage"] for s in cost["stages"]} == {"classify", "extract"}

            audit_result = await session.call_tool("get_audit", {"run_id": run_id})
            audit = _unwrap(audit_result.structured_content)
            assert any(e["event_type"] == "register_entry_committed" for e in audit)

        # Compare against the same corpus's register over plain HTTP.
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            http_resp = await client.get(f"/corpora/{corpus_id}/register")
            assert http_resp.status_code == 200
            http_register = http_resp.json()

        assert len(mcp_register) == len(http_register) == 1
        mcp_entry, http_entry = mcp_register[0], http_register[0]
        assert mcp_entry["feature_key"] == http_entry["feature_key"] == "feature-z"
        assert mcp_entry["fields"] == http_entry["fields"]
        assert mcp_entry["fields"]["owner"] == "Dana Kim"
        assert mcp_entry["fields"]["target_release"] == "v3.0"

    finally:
        if run_id or corpus_id:
            async with AsyncSessionLocal() as session:
                if run_id:
                    await session.execute(delete(Review).where(Review.run_id == uuid.UUID(run_id)))
                    await session.execute(
                        delete(AuditEvent).where(AuditEvent.run_id == uuid.UUID(run_id))
                    )
                    await session.execute(
                        delete(CostEvent).where(CostEvent.run_id == uuid.UUID(run_id))
                    )
                if corpus_id:
                    cid = uuid.UUID(corpus_id)
                    await session.execute(
                        delete(RegisterFieldSource).where(
                            RegisterFieldSource.register_entry_id.in_(
                                select(RegisterEntry.id).where(RegisterEntry.corpus_id == cid)
                            )
                        )
                    )
                    await session.execute(
                        delete(RegisterEntry).where(RegisterEntry.corpus_id == cid)
                    )
                    await session.execute(
                        delete(ClaimSource).where(
                            ClaimSource.claim_id.in_(select(Claim.id).where(Claim.corpus_id == cid))
                        )
                    )
                    await session.execute(delete(Claim).where(Claim.corpus_id == cid))
                    await session.execute(
                        delete(Chunk).where(
                            Chunk.document_id.in_(
                                select(Document.id).where(Document.corpus_id == cid)
                            )
                        )
                    )
                    await session.execute(delete(Document).where(Document.corpus_id == cid))
                    await session.execute(delete(Run).where(Run.corpus_id == cid))
                    await session.execute(delete(Corpus).where(Corpus.id == cid))
                await session.commit()
