"""LangGraph state machine (implementation plan section 8).

Builds the agent graph and exposes `run_agent(run_id, corpus_id, kind)`,
the single entrypoint `backend/app/worker.py` calls to drive one run.
Compiled with `AsyncPostgresSaver` so the graph is checkpointed after each
node; a killed-and-restarted worker calling `run_agent` again for the same
`run_id` resumes from the last completed node instead of re-running the
graph from scratch (CLAUDE.md behavior 2 -- see `test_agent_resume.py`).

Only two placeholder nodes exist so far: `start` and `finish`. Real nodes
(classify, extract, detect_conflicts, examine, build_register, human_gate,
commit -- plan section 8) replace/extend this in later steps.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.app.agent.state import AgentState
from backend.app.config import get_settings
from backend.app.db import AsyncSessionLocal
from backend.app.models.run import Run

logger = structlog.get_logger(__name__)


async def _start_node(state: AgentState) -> dict:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="start")
    logger.info("agent_node_exit", run_id=str(state.run_id), node="start")
    return {}


async def _finish_node(state: AgentState) -> dict:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="finish")
    logger.info("agent_node_exit", run_id=str(state.run_id), node="finish")
    return {}


def build_graph() -> StateGraph:
    """Build (but do not compile) START -> start -> finish -> END.

    Called fresh by every `run_agent` invocation rather than compiled once
    at import time, so `_start_node`/`_finish_node` are looked up by name
    from this module's globals at call time -- tests that monkeypatch
    either name (e.g. `test_agent_resume.py`) take effect on the next
    `run_agent` call without needing a process restart.
    """
    graph = StateGraph(AgentState)
    graph.add_node("start", _start_node)
    graph.add_node("finish", _finish_node)
    graph.add_edge(START, "start")
    graph.add_edge("start", "finish")
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

    On success, flips `runs.status` to `'done'`. On failure, status is
    left as whatever the caller set before calling this (worker.py sets
    `'running'`) -- an honest reflection of "crashed mid-flight", and
    exactly the state a real `kill -9` would leave behind.
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
