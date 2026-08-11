"""LangGraph state machine (implementation plan section 8).

Builds the agent graph and exposes `run_agent(run_id, corpus_id, kind)`,
the single entrypoint `backend/app/worker.py` calls to drive one run.
Compiled with `AsyncPostgresSaver` so the graph is checkpointed after each
node; a killed-and-restarted worker calling `run_agent` again for the same
`run_id` resumes from the last completed node instead of re-running the
graph from scratch (CLAUDE.md behavior 2 -- see `test_agent_resume.py`).

`classify` (agent/nodes/classify.py, plan 8.1), `extract`
(agent/nodes/extract.py, plan 8.2), `detect_conflicts`
(agent/nodes/detect_conflicts.py, plan 8.3), `examine`
(agent/nodes/examine.py, plan 8.4), `build_register`
(agent/nodes/build_register.py, plan 8.5), `human_gate`
(agent/nodes/human_gate.py, plan 8.6), and `commit`
(agent/nodes/commit.py, plan 8.7) are the real nodes; `finish` is still a
trivial placeholder after `commit`.

`resume_run` and `get_agent_state` (task_breakdown Step 25, behavior 3) are
the two other entrypoints this module exposes: `api/reviews.py`'s
`POST /runs/{id}/resume` and `GET`/`POST /runs/{id}/review` call them
respectively, rather than driving the checkpointer directly, so route
handlers stay thin (CLAUDE.md).
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from backend.app.agent.instrumentation import instrument
from backend.app.agent.nodes.build_register import build_register_node
from backend.app.agent.nodes.classify import classify_node, classify_review_node
from backend.app.agent.nodes.commit import commit_node
from backend.app.agent.nodes.detect_conflicts import detect_conflicts_node
from backend.app.agent.nodes.examine import examine_node
from backend.app.agent.nodes.extract import extract_node
from backend.app.agent.nodes.human_gate import human_gate_node
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

        START -> classify -+-> extract -> detect_conflicts -> examine -> build_register -> human_gate -> commit -> finish -> END
                            +-> classify_review -> extract -> detect_conflicts -> examine -> build_register -> human_gate -> commit -> finish -> END

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

    `human_gate` (8.6) may call `interrupt()`, pausing the graph before
    `commit` (8.7) ever runs -- see `run_agent`/`resume_run` below for how
    an interrupted invocation is told apart from a completed one.
    """
    graph = StateGraph(AgentState)
    graph.add_node("classify", instrument("classify", classify_node))
    graph.add_node("classify_review", instrument("classify_review", classify_review_node))
    graph.add_node("extract", instrument("extract", extract_node))
    graph.add_node("detect_conflicts", instrument("detect_conflicts", detect_conflicts_node))
    graph.add_node("examine", instrument("examine", examine_node))
    graph.add_node("build_register", instrument("build_register", build_register_node))
    graph.add_node("human_gate", instrument("human_gate", human_gate_node))
    graph.add_node("commit", instrument("commit", commit_node))
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
    graph.add_edge("build_register", "human_gate")
    graph.add_edge("human_gate", "commit")
    graph.add_edge("commit", "finish")
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

    If the graph pauses at `human_gate`'s `interrupt()` (behavior 3), this
    returns without marking the run done: `human_gate_node` already flipped
    `runs.status` to `'awaiting_review'` before interrupting, and that's the
    honest state until `resume_run` (below) drives it the rest of the way.
    Detected via `aget_state(config).next` -- non-empty means there's still
    a pending node (the interrupted one), same check `resume_run` uses.
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
        current_state = await graph.aget_state(config)

    if current_state.next:
        logger.info("agent_run_awaiting_review", run_id=str(run_id), next=current_state.next)
        return

    await _mark_run_done(run_id)


async def resume_run(run_id: uuid.UUID) -> None:
    """Continue a run paused at `human_gate`'s `interrupt()`
    (task_breakdown Step 25, behavior 3), driven by `POST /runs/{id}/resume`.

    Requires `Command(resume=...)` rather than a plain `None` input --
    unlike `run_agent`'s crash-resume path, which just continues the graph
    past a completed node, a paused `interrupt()` call specifically needs a
    resume value or it raises again immediately (LangGraph matches resume
    values to `interrupt()` calls by order within the node; see
    `langgraph.types.interrupt`). The value itself is never read by
    `human_gate_node` -- it re-derives every decision from the `reviews`
    table on each execution (see that node's docstring) -- so `True` here
    is just a non-`None` placeholder to satisfy that requirement.
    """
    settings = get_settings()
    conn_string = _psycopg_conn_string(settings.DATABASE_URL)

    async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        await checkpointer.setup()
        graph: CompiledStateGraph = build_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(run_id)}}

        await graph.ainvoke(Command(resume=True), config=config)
        current_state = await graph.aget_state(config)

    if current_state.next:
        logger.info("agent_run_still_awaiting_review", run_id=str(run_id), next=current_state.next)
        return

    await _mark_run_done(run_id)


async def get_agent_state(run_id: uuid.UUID) -> AgentState | None:
    """The current checkpointed `AgentState` for `run_id`, or `None` if the
    graph has never been invoked for it. Used by `api/reviews.py` to read
    `register_diff` for `GET`/`POST /runs/{id}/review` without duplicating
    checkpointer plumbing in the route layer."""
    settings = get_settings()
    conn_string = _psycopg_conn_string(settings.DATABASE_URL)

    async with AsyncPostgresSaver.from_conn_string(conn_string) as checkpointer:
        await checkpointer.setup()
        graph: CompiledStateGraph = build_graph().compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": str(run_id)}}
        snapshot = await graph.aget_state(config)

    if not snapshot.values:
        return None
    return AgentState(**snapshot.values)
