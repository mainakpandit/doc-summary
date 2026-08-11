"""LangGraph state machine (implementation plan section 8).

Builds the agent graph and exposes `run_agent(run_id, corpus_id, kind)`,
the single entrypoint `backend/app/worker.py` calls to drive one run.
Compiled with `AsyncPostgresSaver` so the graph is checkpointed after each
node; a killed-and-restarted worker calling `run_agent` again for the same
`run_id` resumes from the last completed node instead of re-running the
graph from scratch (CLAUDE.md behavior 2 -- see `test_agent_resume.py`).

`classify` (agent/nodes/classify.py, plan 8.1) is the first real node and
replaces the old `start` placeholder as the graph's entry point; `extract`
(agent/nodes/extract.py, plan 8.2), `detect_conflicts`
(agent/nodes/detect_conflicts.py, plan 8.3), `examine`
(agent/nodes/examine.py, plan 8.4), and `build_register`
(agent/nodes/build_register.py, plan 8.5) follow. `finish` is still a
placeholder. Real nodes (human_gate, commit -- plan section 8) extend this
in later steps.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.app.agent.instrumentation import instrument
from backend.app.agent.nodes.build_register import build_register_node
from backend.app.agent.nodes.classify import classify_node, classify_review_node
from backend.app.agent.nodes.detect_conflicts import detect_conflicts_node
from backend.app.agent.nodes.examine import examine_node
from backend.app.agent.nodes.extract import extract_node
from backend.app.agent.state import AgentState
from backend.app.config import get_settings
from backend.app.db import AsyncSessionLocal
from backend.app.models.run import Run
from backend.app.services.events import emit

logger = structlog.get_logger(__name__)


def _route_after_classify(state: AgentState) -> Literal["classify_review", "extract"]:
    return "classify_review" if state.needs_classification_review else "extract"


async def _finish_node(state: AgentState) -> dict:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="finish")
    logger.info("agent_node_exit", run_id=str(state.run_id), node="finish")
    return {}


def build_graph() -> StateGraph:
    """Build (but do not compile) the graph:

        START -> classify -+-> extract -> detect_conflicts -> examine -> build_register -> finish -> END
                            +-> classify_review -> extract -> detect_conflicts -> examine -> build_register -> finish -> END

    `classify` routes to `classify_review` only when it set
    `state.needs_classification_review`; otherwise it goes straight to
    `extract`, and `classify_review` falls through to `extract` too --
    escalation is a soft flag for a human, not a hard stop (see
    `agent/nodes/classify.py`'s `classify_review_node` docstring). Called
    fresh by every `run_agent` invocation rather than compiled once at
    import time, so `classify_node`/`_finish_node` are looked up by name
    from this module's globals at call time -- tests that monkeypatch
    either name (e.g. `test_agent_resume.py`) take effect on the next
    `run_agent` call without needing a process restart.

    Every node is wrapped with `instrument` (CLAUDE.md behavior 1) so it
    emits a `<name>_start` / `<name>_end` audit + SSE event on entry and
    exit; this wrapping happens here, fresh, on every call too, so a
    monkeypatched node (e.g. `_finish_node` in `test_agent_resume.py`) is
    still the function `instrument` wraps.
    """
    graph = StateGraph(AgentState)
    graph.add_node("classify", instrument("classify", classify_node))
    graph.add_node("classify_review", instrument("classify_review", classify_review_node))
    graph.add_node("extract", instrument("extract", extract_node))
    graph.add_node("detect_conflicts", instrument("detect_conflicts", detect_conflicts_node))
    graph.add_node("examine", instrument("examine", examine_node))
    graph.add_node("build_register", instrument("build_register", build_register_node))
    graph.add_node("finish", instrument("finish", _finish_node))
    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        _route_after_classify,
        {"classify_review": "classify_review", "extract": "extract"},
    )
    graph.add_edge("classify_review", "extract")
    graph.add_edge("extract", "detect_conflicts")
    graph.add_edge("detect_conflicts", "examine")
    graph.add_edge("examine", "build_register")
    graph.add_edge("build_register", "finish")
    graph.add_edge("finish", END)
    return graph


def _psycopg_conn_string(database_url: str) -> str:
    """AsyncPostgresSaver connects via psycopg, which doesn't understand
    SQLAlchemy's `+asyncpg` driver suffix in DATABASE_URL -- strip it."""
    scheme, _, rest = database_url.partition("://")
    return f"{scheme.split('+', 1)[0]}://{rest}"


async def _mark_run_done(run_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as session:
        run = await session.get(Run, run_id)
        if run is not None:
            run.status = "done"
            await session.commit()
        await emit(session, run_id, "run_completed", {"status": "done"})


async def run_agent(
    run_id: uuid.UUID, corpus_id: uuid.UUID, kind: Literal["initial", "update"]
) -> None:
    """Drive one run through the graph, using `run_id` as the LangGraph
    `thread_id`.

    If this `run_id` has never been invoked before, starts the graph with
    a fresh `AgentState`. If it has (a prior call started the graph and
    then crashed, or the process was killed and restarted), resumes from
    the last checkpoint by invoking with `None` instead of a fresh input
    -- passing a fresh input again would re-enter at START and re-run
    every node, including ones already completed and checkpointed.

    On success, flips `runs.status` to `'done'` and emits a terminal
    `run_completed` audit + SSE event -- the signal `GET /runs/{id}/events`
    consumers (task_breakdown Step 24) watch for to know the stream is
    finished. On failure, status is left as whatever the caller set before
    calling this (worker.py sets `'running'`) -- an honest reflection of
    "crashed mid-flight", and exactly the state a real `kill -9` would
    leave behind; no terminal event is emitted, so a live SSE client
    watching a crashed run simply stops receiving events rather than
    seeing a false "completed".
    """
    settings = get_settings()
    conn_string = _psycopg_conn_string(settings.DATABASE_URL)

    async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        await checkpointer.setup()
        graph: CompiledStateGraph = build_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(run_id)}}

        existing = await graph.aget_state(config)
        graph_input = (
            None
            if existing.values
            else AgentState(run_id=run_id, corpus_id=corpus_id, kind=kind, trigger_doc_id=None)
        )

        await graph.ainvoke(graph_input, config=config)

    await _mark_run_done(run_id)
