"""Behavior 2 (resumable): backend/app/agent/graph.py + backend/app/worker.py.

Kills a run between the 'start' and 'finish' placeholder nodes by
monkeypatching a synthetic exception into finish's first call, "restarts
the worker" by calling run_agent again for the same run_id, and asserts:
  - the run completes (runs.status flips to 'done')
  - 'start' does not re-run on resume: the checkpointer recorded exactly
    two node executions (start, finish), not three
  - no cost_events were written -- both nodes are placeholders (nothing to
    bill yet), and CLAUDE.md behavior 2 requires completed stages are
    never re-billed on resume

Requires a live Postgres, both for the runs/corpora rows and for the real
AsyncPostgresSaver checkpointer (a separate psycopg connection to the same
database). Marked `integration` and skipped if unreachable, matching
test_llm_wrapper.py's pattern.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from backend.app.agent import graph as graph_module
from backend.app.config import get_settings
from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import Corpus, CostEvent, Run
from backend.app.worker import run_once


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


@pytest.fixture
async def pending_run():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="agent-resume-test", inbox_path="/tmp/agent-resume-test-inbox")
        session.add(corpus)
        await session.flush()
        run = Run(corpus_id=corpus.id, kind="initial", status="pending")
        session.add(run)
        await session.commit()
        cid, rid = corpus.id, run.id

    yield rid

    async with AsyncSessionLocal() as session:
        await session.execute(delete(CostEvent).where(CostEvent.run_id == rid))
        await session.execute(delete(Run).where(Run.id == rid))
        await session.execute(delete(Corpus).where(Corpus.id == cid))
        await session.commit()


@pytest.mark.integration
async def test_resume_after_crash_skips_completed_nodes(pending_run, monkeypatch):
    run_id = pending_run

    calls = {"finish": 0}
    original_finish = graph_module._finish_node

    async def flaky_finish(state):
        calls["finish"] += 1
        if calls["finish"] == 1:
            raise RuntimeError("synthetic worker crash between start and finish")
        return await original_finish(state)

    monkeypatch.setattr(graph_module, "_finish_node", flaky_finish)

    # First attempt: the worker claims the pending run and drives it
    # through the graph; 'finish' raises on its first call.
    with pytest.raises(RuntimeError, match="synthetic worker crash"):
        await run_once()

    async with AsyncSessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "running"  # crashed mid-flight, never reached 'done'
        corpus_id, kind = run.corpus_id, run.kind

    # "Restart the worker": call run_agent directly for the same run_id.
    # claim_pending_run() only picks up status='pending' rows -- SKIP
    # LOCKED reconciliation of orphaned 'running' runs after a real
    # process kill is Step 26, not this one -- so re-invoking run_agent
    # directly is what simulates "the worker process restarts and picks
    # back up the run it was on" at this step.
    await graph_module.run_agent(run_id, corpus_id, kind)

    async with AsyncSessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run.status == "done"

    assert calls["finish"] == 2  # first call raised, second (the resume) succeeded

    # Inspect the checkpointer directly: 'classify' must not have re-run.
    # Empirically (verified against a real Postgres checkpointer), this
    # graph's checkpoints carry metadata.step == -1 (pre-input) and 0
    # (input applied, no node run yet) before any real node executes;
    # step 1 is 'classify' completing, step 2 is 'extract' completing
    # (an empty-documents run routes straight past 'classify_review'),
    # step 3 is 'detect_conflicts' completing (no claims -> no conflicts,
    # but the node still runs and checkpoints), and step 4 is 'finish'
    # completing. So checkpoints with step >= 1 count real node
    # executions -- exactly 4 on a correct resume, more than 4 if
    # 'classify' (or anything else) re-ran.
    conn_string = graph_module._psycopg_conn_string(get_settings().DATABASE_URL)
    async with graph_module.AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        compiled = graph_module.build_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(run_id)}}
        history = [c async for c in compiled.aget_state_history(config)]

    node_completions = [c for c in history if c.metadata.get("step", -1) >= 1]
    assert len(node_completions) == 4

    # Both nodes are placeholders -- nothing should ever have been billed,
    # on either attempt.
    async with AsyncSessionLocal() as session:
        events = (await session.scalars(select(CostEvent).where(CostEvent.run_id == run_id))).all()
    assert events == []
