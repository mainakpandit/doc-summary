"""Tiny worker entrypoint (`python -m backend.app.worker`).

Picks up at most one `pending` run, flips it to `running`, and drives it
through `agent.graph.run_agent`. This is intentionally minimal: concurrent
polling with `SELECT ... FOR UPDATE SKIP LOCKED` across multiple worker
processes is CLAUDE.md behavior 9 / task_breakdown.md Step 26, not this
step.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

import structlog
from sqlalchemy import select

from backend.app.agent.graph import run_agent
from backend.app.db import AsyncSessionLocal
from backend.app.models.run import Run

logger = structlog.get_logger(__name__)


async def claim_pending_run() -> tuple[uuid.UUID, uuid.UUID, Literal["initial", "update"]] | None:
    """Mark the oldest `pending` run `running` and return its identity, or
    `None` if there is no pending run."""
    async with AsyncSessionLocal() as session:
        run = (
            await session.scalars(
                select(Run).where(Run.status == "pending").order_by(Run.started_at).limit(1)
            )
        ).first()
        if run is None:
            return None
        run.status = "running"
        await session.commit()
        return run.id, run.corpus_id, run.kind


async def run_once() -> uuid.UUID | None:
    """Claim and run one pending run, if any. Returns its `run_id`, or
    `None` if there was nothing pending. Propagates any exception from
    `run_agent` rather than swallowing it -- the run's checkpoint already
    captured everything completed so far (behavior 2); there's nothing
    useful to do here except let the caller see the failure."""
    claimed = await claim_pending_run()
    if claimed is None:
        logger.info("worker_no_pending_runs")
        return None

    run_id, corpus_id, kind = claimed
    logger.info("worker_run_claimed", run_id=str(run_id), corpus_id=str(corpus_id), kind=kind)
    await run_agent(run_id, corpus_id, kind)
    logger.info("worker_run_done", run_id=str(run_id))
    return run_id


async def main() -> None:
    await run_once()


if __name__ == "__main__":
    asyncio.run(main())
