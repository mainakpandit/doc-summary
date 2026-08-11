"""`human_gate` node (implementation plan 8.6, task_breakdown Step 25,
behavior 3).

Pauses the graph with LangGraph's `interrupt()` so a human can approve or
reject every unresolved conflict, pending finding, and proposed register
change before `commit` (agent/nodes/commit.py) writes anything real.
Setting `runs.status = 'awaiting_review'` happens here, right before the
interrupt, rather than in `graph.run_agent` -- this node is the only place
that knows *whether* there's actually anything to review, and the status
flip needs to be durably committed before the interrupt suspends execution.

LangGraph re-executes this whole function from the top on every resume
(`langgraph.types.interrupt`'s docstring: "The graph resumes from the start
of the node, re-executing all logic") -- there's no way to pick up
mid-function. So instead of carrying state between the pre-interrupt and
post-interrupt halves of one execution, every pass independently re-derives
the *current* set of pending items from the database
(`services/review.build_review_payload`, the same function
`GET /runs/{id}/review` calls) and from `reviews` rows already written by
`POST /runs/{id}/review`. That makes every pass idempotent: a pass with
nothing left pending returns normally without calling `interrupt()` at all,
and the graph moves on to `commit`; a pass with something still
pending -- the very first pass, or a resume after only a partial batch of
decisions -- interrupts again with whatever's still undecided. The `while`
loop exists only for that second case.
"""

from __future__ import annotations

from typing import Any

import structlog
from langgraph.types import interrupt

from backend.app.agent.state import AgentState
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.run import Run
from backend.app.services import review as review_service

logger = structlog.get_logger(__name__)


async def human_gate_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="human_gate")

    while True:
        async with AsyncSessionLocal() as session:
            payload = await review_service.build_review_payload(
                session, state.run_id, state.register_diff
            )

            if payload.is_empty:
                session.add(
                    AuditEvent(run_id=state.run_id, event_type="human_gate_clean", payload={})
                )
                await session.commit()
                logger.info(
                    "agent_node_exit", run_id=str(state.run_id), node="human_gate", gated=False
                )
                return {"register_diff": payload.register_diff}

            run = await session.get(Run, state.run_id)
            run.status = "awaiting_review"
            await session.commit()

        logger.info(
            "agent_node_exit",
            run_id=str(state.run_id),
            node="human_gate",
            gated=True,
            conflicts=len(payload.conflicts),
            findings=len(payload.findings),
            register_changes=len(payload.register_changes),
        )
        interrupt(payload.as_dict())
