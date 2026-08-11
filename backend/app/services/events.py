"""In-memory SSE pub/sub keyed by run_id (CLAUDE.md: one database, no
Redis -- this lives entirely in the API process's memory, matching
task_breakdown Step 24: "GET /runs/{id}/events streaming SSE events from
an in-memory pub/sub keyed by run_id").

`emit` is the single place that writes an audit event and fans the
identical payload out to every live subscriber in the same call, so
CLAUDE.md behavior 1's "every agent node emits SSE + audit events on
entry and exit" holds with the SSE payload matching the audit event by
construction, not by convention. `agent/instrumentation.py` is the only
caller that uses this for node entry/exit; nodes' own internal audit
writes (e.g. `classify_escalated`, `register_entry_proposed`) go straight
to `AuditEvent` since those aren't part of "every node's SSE envelope",
just each node's own business detail.

`stream_run_events` is what `api/runs.py`'s `GET /runs/{id}/events` route
consumes: it first replays everything already in `audit_events` for the
run (so a client connecting after the run finished -- or after it's
partway through -- still sees the full history), then, only if the run
hasn't reached a terminal status yet, subscribes to the live queue and
keeps yielding until a terminal event arrives. It opens its own session
via `AsyncSessionLocal` rather than accepting one as a parameter: an
`EventSourceResponse` generator runs after the route handler returns, by
which point a `Depends(get_session)`-scoped session has already been
closed, so it can't be threaded through safely.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.run import Run

TERMINAL_RUN_STATUSES = {"done", "failed", "cancelled"}
TERMINAL_EVENT_TYPES = {"run_completed", "run_failed"}

_subscribers: dict[uuid.UUID, set[asyncio.Queue]] = defaultdict(set)


def subscribe(run_id: uuid.UUID) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    _subscribers[run_id].add(queue)
    return queue


def unsubscribe(run_id: uuid.UUID, queue: asyncio.Queue) -> None:
    _subscribers[run_id].discard(queue)
    if not _subscribers[run_id]:
        _subscribers.pop(run_id, None)


def publish(run_id: uuid.UUID, event_type: str, payload: dict[str, Any]) -> None:
    for queue in list(_subscribers.get(run_id, ())):
        queue.put_nowait({"event_type": event_type, "payload": payload})


async def emit(
    session: AsyncSession, run_id: uuid.UUID, event_type: str, payload: dict[str, Any]
) -> None:
    session.add(AuditEvent(run_id=run_id, event_type=event_type, payload=payload))
    await session.commit()
    publish(run_id, event_type, payload)


async def stream_run_events(run_id: uuid.UUID) -> AsyncIterator[dict[str, Any]]:
    async with AsyncSessionLocal() as session:
        past = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.id)
            )
        ).all()
        for event in past:
            yield {"event_type": event.event_type, "payload": event.payload}

        run = await session.get(Run, run_id)
        already_terminal = run is None or run.status in TERMINAL_RUN_STATUSES

    if already_terminal:
        return

    queue = subscribe(run_id)
    try:
        while True:
            item = await queue.get()
            yield item
            if item["event_type"] in TERMINAL_EVENT_TYPES:
                return
    finally:
        unsubscribe(run_id, queue)
