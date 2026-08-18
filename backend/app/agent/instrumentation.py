"""Wraps every graph node so it emits a `<name>_start` / `<name>_end`
audit event (CLAUDE.md behavior 1: "Every agent node emits SSE + audit
events on entry and exit") without each node file doing this itself.
Built as a thin wrapper -- not folded into each node function -- so a
node's own internal audit writes stay about *what it decided*
(`classify_escalated`, `register_entry_proposed`, ...) while this wrapper
only ever answers *whether the node is running*; a live progress UI and a
human reading the audit trail for "what changed and why" don't need the
same events.

`services.events.emit` writes the audit_events row and publishes the same
payload over the in-memory SSE pub/sub in one call, so "the SSE payload
matches the audit event" holds without this module touching two separate
code paths.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from backend.app.agent.state import AgentState
from backend.app.db import AsyncSessionLocal
from backend.app.services.events import emit

NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def instrument(name: str, node: NodeFn) -> NodeFn:
    """Return a node function that emits `{name}_start` before calling
    `node`, then `{name}_end` (carrying the keys `node` returned) after."""

    async def wrapped(state: AgentState) -> dict[str, Any]:
        async with AsyncSessionLocal() as session:
            await emit(session, state.run_id, f"{name}_start", {"node": name})

        result = await node(state)

        async with AsyncSessionLocal() as session:
            await emit(
                session,
                state.run_id,
                f"{name}_end",
                {"node": name, "updated_keys": sorted(result.keys())},
            )

        return result

    wrapped.__name__ = f"instrumented_{name}"
    return wrapped
