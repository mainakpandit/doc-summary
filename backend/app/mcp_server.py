"""MCP entrypoint (implementation plan section 11, task_breakdown Step 30
(3), CLAUDE.md behavior 4: "Machine drivable").

Every tool below is a thin wrapper over the exact same `services/*`
functions `backend/app/api/*.py`'s HTTP routes call -- no business logic
lives here, matching CLAUDE.md's "HTTP and MCP call the same service layer;
no logic in route handlers" (this module is that rule's MCP-side route
layer). Each tool opens its own `AsyncSessionLocal()` rather than sharing
one across the process, the same "one session per unit of work" shape
`api/*.py`'s `Depends(get_session)` gives HTTP routes.

Driving the agent graph itself (`agent.graph.run_agent`) is deliberately
*not* exposed as a tool: `start_run` only inserts a `pending` `runs` row,
exactly like `POST /runs`, and a worker process (`python -m
backend.app.worker`) is what actually claims and runs it -- machine-drivable
means a machine can drive the *workflow* (create a corpus, add documents,
start a run, review, resume, read the register/cost/audit), not that it
reimplements the worker.

Run as `python -m backend.app.mcp_server` (stdio transport, the default
`MCPServer.run()` picks and the transport `test_mcp.py` spawns this module
as a subprocess over).
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from backend.app.agent.graph import get_agent_state
from backend.app.agent.graph import resume_run as resume_run_graph
from backend.app.db import AsyncSessionLocal
from backend.app.models.corpus import Corpus
from backend.app.models.run import Run
from backend.app.schemas.review import ReviewItemDecision
from backend.app.services import corpora as corpora_service
from backend.app.services import register as register_service
from backend.app.services import review as review_service
from backend.app.services import runs as runs_service
from backend.app.services.ingestion import ingest_file
from backend.app.services.parsers import UnsupportedFormat

mcp: MCPServer = MCPServer("pm-analyst")


def _corpus_dict(corpus: Corpus) -> dict[str, Any]:
    return {
        "id": str(corpus.id),
        "name": corpus.name,
        "inbox_path": corpus.inbox_path,
        "rules_path": corpus.rules_path,
        "created_at": corpus.created_at.isoformat() if corpus.created_at else None,
    }


def _run_dict(run: Run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "corpus_id": str(run.corpus_id),
        "kind": run.kind,
        "status": run.status,
        "triggering_document_id": (
            str(run.triggering_document_id) if run.triggering_document_id else None
        ),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error": run.error,
    }


@mcp.tool()
async def list_corpora() -> list[dict[str, Any]]:
    """List every corpus, newest first."""
    async with AsyncSessionLocal() as session:
        corpora = await corpora_service.list_corpora(session)
        return [_corpus_dict(c) for c in corpora]


@mcp.tool()
async def create_corpus(
    name: str, inbox_path: str, rules_path: str | None = None
) -> dict[str, Any]:
    """Create a corpus: a named collection of documents with one Feature
    Register. `inbox_path` is the folder `services/watcher.py` watches for
    this corpus's incremental updates."""
    async with AsyncSessionLocal() as session:
        corpus = await corpora_service.create_corpus(session, name, inbox_path, rules_path)
        return _corpus_dict(corpus)


@mcp.tool()
async def add_document(corpus_id: str, file_path: str) -> dict[str, Any]:
    """Ingest a file already on disk (readable by the MCP server process)
    into a corpus: parses, chunks, and persists it, deduping on content
    hash. Mirrors `POST /corpora/{id}/documents`, minus the HTTP upload
    step -- callers here already have the file on a filesystem the server
    can read."""
    async with AsyncSessionLocal() as session:
        corpus = await session.get(Corpus, uuid.UUID(corpus_id))
        if corpus is None:
            raise ValueError(f"corpus {corpus_id} not found")

        try:
            document = await ingest_file(session, Path(file_path), corpus.id)
        except UnsupportedFormat as exc:
            raise ValueError(str(exc)) from exc

        return {
            "id": str(document.id),
            "corpus_id": str(document.corpus_id),
            "filename": document.filename,
            "content_hash": document.content_hash,
            "mime_type": document.mime_type,
            "doc_type": document.doc_type,
        }


@mcp.tool()
async def start_run(corpus_id: str, kind: Literal["initial", "update"]) -> dict[str, Any]:
    """Enqueue a `pending` run for a corpus. A separate worker process
    (`python -m backend.app.worker`) claims and drives it -- this tool only
    creates the row, exactly like `POST /runs`."""
    async with AsyncSessionLocal() as session:
        corpus = await session.get(Corpus, uuid.UUID(corpus_id))
        if corpus is None:
            raise ValueError(f"corpus {corpus_id} not found")
        run, _created = await runs_service.create_run(session, corpus.id, kind)
        return _run_dict(run)


@mcp.tool()
async def get_run(run_id: str) -> dict[str, Any]:
    """A run's current status, pipeline stage, and claim/conflict/finding
    counts."""
    async with AsyncSessionLocal() as session:
        run = await runs_service.get_run(session, uuid.UUID(run_id))
        if run is None:
            raise ValueError(f"run {run_id} not found")
        stage = await runs_service.current_stage(session, run.id)
        counts = await runs_service.run_counts(session, run.id)
        return {**_run_dict(run), "current_stage": stage, "counts": counts}


@mcp.tool()
async def list_review_items(run_id: str) -> dict[str, Any]:
    """Conflicts, findings, and proposed register changes awaiting human
    review for a run paused at the human gate. Mirrors
    `GET /runs/{id}/review`'s body."""
    rid = uuid.UUID(run_id)
    async with AsyncSessionLocal() as session:
        state = await get_agent_state(rid)
        register_diff = state.register_diff if state else None
        payload = await review_service.build_review_payload(session, rid, register_diff)
        return payload.as_dict()


@mcp.tool()
async def submit_review(
    run_id: str, decisions: list[dict[str, Any]], reviewer: str
) -> dict[str, Any]:
    """Approve or reject pending review items. Each decision is
    `{"id": ..., "item_type": "conflict"|"finding"|"register_change",
    "decision": "approve"|"reject", "note": optional}`. Approving or
    rejecting one item never affects its siblings (CLAUDE.md behavior 3).
    Mirrors `POST /runs/{id}/review`."""
    rid = uuid.UUID(run_id)
    items = [
        ReviewItemDecision(
            id=uuid.UUID(d["id"]),
            item_type=d["item_type"],
            decision=d["decision"],
            note=d.get("note"),
        )
        for d in decisions
    ]
    async with AsyncSessionLocal() as session:
        await review_service.submit_review_decisions(session, rid, items, reviewer)
    return {"accepted": len(items)}


@mcp.tool()
async def resume_run(run_id: str) -> dict[str, Any]:
    """Resume a run paused at the human review gate, applying every
    decision recorded by `submit_review` and continuing the graph to
    completion (or to its next gate, if more items are still pending).
    Mirrors `POST /runs/{id}/resume`."""
    rid = uuid.UUID(run_id)
    await resume_run_graph(rid)
    async with AsyncSessionLocal() as session:
        run = await runs_service.get_run(session, rid)
        if run is None:
            raise ValueError(f"run {run_id} not found")
        return _run_dict(run)


@mcp.tool()
async def get_register(corpus_id: str) -> list[dict[str, Any]]:
    """The Feature Register for a corpus: one entry per feature, with
    per-field backing claims and citations. Mirrors
    `GET /corpora/{id}/register`."""
    async with AsyncSessionLocal() as session:
        entries = await register_service.list_register_entries(session, uuid.UUID(corpus_id))
        return [
            {
                "id": str(e.id),
                "feature_key": e.feature_key,
                "fields": e.fields,
                "field_claims": e.field_claims,
                "version": e.version,
                "updated_at": e.updated_at.isoformat() if e.updated_at else None,
            }
            for e in entries
        ]


@mcp.tool()
async def get_cost(run_id: str) -> dict[str, Any]:
    """Per-stage token/latency/USD cost breakdown for a run, plus the
    total. Mirrors `GET /runs/{id}/cost`."""
    async with AsyncSessionLocal() as session:
        cost = await runs_service.get_run_cost(session, uuid.UUID(run_id))
        return {
            "run_id": str(cost["run_id"]),
            "total_usd_cost": float(cost["total_usd_cost"]),
            "stages": [{**stage, "usd_cost": float(stage["usd_cost"])} for stage in cost["stages"]],
        }


@mcp.tool()
async def get_audit(run_id: str) -> list[dict[str, Any]]:
    """The full audit trail for a run, in occurred order -- what changed,
    when, because of which source. Mirrors `GET /runs/{id}/audit`."""
    async with AsyncSessionLocal() as session:
        events = await runs_service.get_run_audit(session, uuid.UUID(run_id))
        return [
            {
                "id": e.id,
                "run_id": str(e.run_id) if e.run_id else None,
                "event_type": e.event_type,
                "payload": e.payload,
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            }
            for e in events
        ]


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
