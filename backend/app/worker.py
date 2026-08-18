"""Long-running worker process (`python -m backend.app.worker`).

`Worker` polls `runs` for `pending` rows once a second (CLAUDE.md behavior
9 / TASK_BREAKDOWN.md Step 26), claims up to `settings.MAX_CONCURRENT_RUNS`
minus however many it already has in flight via
`SELECT ... FOR UPDATE SKIP LOCKED`, and drives each claimed run through
`agent.graph.run_agent` concurrently via `asyncio.create_task`. Any number
of worker processes can run this loop against the same database at once:
`SKIP LOCKED` means a row locked by one process's claiming transaction is
simply skipped by another's, so a given run is always claimed by exactly
one of them, never both (see `backend/tests/test_concurrent.py`).

Same-corpus concurrent register writes are a separate concern, handled by
an advisory lock in `agent/nodes/commit.py` rather than anything here --
see `docs/architecture.md`.

`claim_pending_run` and `run_once` predate this rewrite and are kept
as-is: `test_agent_resume.py` and `test_api_runs.py` both call `run_once()`
directly to simulate "restart the worker and finish this one run",
depending on it claiming exactly one run and propagating `run_agent`'s
exception synchronously so the test can observe a crash. `Worker`, below,
is the real concurrent entrypoint `main()` runs; it deliberately does not
let one run's exception propagate out and kill every other run it's
driving.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal, NamedTuple

import structlog
from sqlalchemy import select

from backend.app.agent.graph import run_agent
from backend.app.config import get_settings
from backend.app.db import AsyncSessionLocal
from backend.app.models.run import Run

logger = structlog.get_logger(__name__)


class ClaimedRun(NamedTuple):
    run_id: uuid.UUID
    corpus_id: uuid.UUID
    kind: Literal["initial", "update"]


async def claim_pending_runs(limit: int) -> list[ClaimedRun]:
    """Claim up to `limit` oldest `pending` runs and flip them to
    `running`, in one transaction so the SELECT's row locks are still held
    for the UPDATE. `FOR UPDATE SKIP LOCKED` means a concurrent caller
    (another worker process, or this same process's next poll) never
    blocks on rows already claimed here -- it just skips them and claims
    whatever's left, so no run is ever claimed twice."""
    if limit <= 0:
        return []

    async with AsyncSessionLocal() as session:
        runs = (
            await session.scalars(
                select(Run)
                .where(Run.status == "pending")
                .order_by(Run.started_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
        claimed = [ClaimedRun(run.id, run.corpus_id, run.kind) for run in runs]
        for run in runs:
            run.status = "running"
        await session.commit()
        return claimed


async def claim_pending_run() -> ClaimedRun | None:
    """Claim a single pending run, or `None` if there is none. Thin
    wrapper over `claim_pending_runs(1)` kept for `run_once` below."""
    claimed = await claim_pending_runs(1)
    return claimed[0] if claimed else None


async def run_once() -> uuid.UUID | None:
    """Claim and run one pending run, if any, synchronously in this
    coroutine. Returns its `run_id`, or `None` if there was nothing
    pending. Propagates any exception from `run_agent` rather than
    swallowing it -- the run's checkpoint already captured everything
    completed so far (behavior 2); there's nothing useful to do here
    except let the caller see the failure."""
    claimed = await claim_pending_run()
    if claimed is None:
        logger.info("worker_no_pending_runs")
        return None

    run_id, corpus_id, kind = claimed
    logger.info("worker_run_claimed", run_id=str(run_id), corpus_id=str(corpus_id), kind=kind)
    await run_agent(run_id, corpus_id, kind)
    logger.info("worker_run_done", run_id=str(run_id))
    return run_id


class Worker:
    """Drives `runs` concurrently, capped at `max_concurrent` in-flight
    `run_agent` calls.

    Call `run_forever()` to poll every `poll_interval` seconds until
    `stop()` is called. A poll also fires immediately whenever an
    in-flight task finishes, rather than waiting out the rest of that
    tick -- `_wake` is set from `_drive`'s `finally` block, and
    `run_forever` waits on it with `poll_interval` as a timeout, so a
    concurrency slot freed by a fast run is reused right away instead of
    sitting idle until the next scheduled poll.
    """

    def __init__(self, max_concurrent: int, poll_interval: float = 1.0) -> None:
        self._max_concurrent = max_concurrent
        self._poll_interval = poll_interval
        self._tasks: set[asyncio.Task] = set()
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    async def poll(self) -> list[uuid.UUID]:
        """Claim as many pending runs as available concurrency slots
        allow and launch each as a tracked task. Returns the claimed
        run_ids."""
        available = self._max_concurrent - len(self._tasks)
        if available <= 0:
            return []

        claimed = await claim_pending_runs(available)
        for run_id, corpus_id, kind in claimed:
            task = asyncio.create_task(self._drive(run_id, corpus_id, kind))
            self._tasks.add(task)
        return [c.run_id for c in claimed]

    async def _drive(
        self, run_id: uuid.UUID, corpus_id: uuid.UUID, kind: Literal["initial", "update"]
    ) -> None:
        logger.info("worker_run_claimed", run_id=str(run_id), corpus_id=str(corpus_id), kind=kind)
        try:
            await run_agent(run_id, corpus_id, kind)
            logger.info("worker_run_done", run_id=str(run_id))
        except Exception:
            # A crashed run leaves runs.status='running' -- an honest
            # "died mid-flight" state, the same one a killed single-run
            # worker would leave (see run_agent's docstring). Swallowed
            # here rather than re-raised: this task is one of possibly
            # several this process is driving concurrently, and one run's
            # failure must not take the others down with it.
            logger.exception("worker_run_failed", run_id=str(run_id))
        finally:
            self._tasks.discard(asyncio.current_task())
            self._wake.set()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            await self.poll()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except TimeoutError:
                pass
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


async def main() -> None:
    settings = get_settings()
    worker = Worker(max_concurrent=settings.MAX_CONCURRENT_RUNS)
    await worker.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
