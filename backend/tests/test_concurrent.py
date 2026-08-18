"""Behavior 9 (concurrent): `backend/app/worker.py`'s `Worker` and the
advisory lock in `backend/app/agent/nodes/commit.py` (TASK_BREAKDOWN.md
Step 26).

Two independent scenarios:

  - `test_two_worker_processes_claim_each_run_exactly_once`: enqueues 10
    `pending` runs against one corpus with no documents (so `run_agent`
    completes without any LLM call -- same zero-document shortcut
    `test_agent_resume.py` relies on), spawns two real OS processes via
    `multiprocessing.get_context("spawn")` each running `Worker.run_forever`
    against the same Postgres, and asserts every run reaches `done` with
    exactly one `classify_start` audit event -- proof `FOR UPDATE SKIP
    LOCKED` never let both processes claim the same row.

  - `test_concurrent_commits_same_corpus_do_not_corrupt_register_entries`:
    runs two `commit_node` calls concurrently (`asyncio.gather`, real
    Postgres, no multiprocessing needed since the race is at the DB
    transaction level, not the process level) against the same
    `register_entries` row, each applying a different field change, and
    asserts both changes and both version increments survive -- proof the
    advisory lock in `commit_node` (see `docs/architecture.md`) prevents
    the lost-update race that would otherwise let the second commit
    silently clobber the first.

Both are marked `integration` and skipped when DATABASE_URL is
unreachable, matching every other node/worker test. The `multiprocessing`
scenario needs its own picklable, module-level target function
(`_worker_process_target`, below) -- the 'spawn' start method re-imports
this module fresh in each child process and calls the target by qualified
name, so a closure or a fixture-scoped function would not survive the
pickle round trip.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import uuid

import pytest
from sqlalchemy import delete, func, select

from backend.app.agent.nodes.commit import commit_node
from backend.app.agent.state import AgentState, RegisterDiff, RegisterFieldChange
from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import (
    AuditEvent,
    Claim,
    Corpus,
    CostEvent,
    RegisterEntry,
    RegisterFieldSource,
    Run,
)
from backend.app.worker import Worker

pytestmark = pytest.mark.integration

NUM_RUNS = 10


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


def _worker_process_target(
    max_concurrent: int, poll_interval: float, timeout_seconds: float
) -> None:
    """Picklable process entrypoint: runs a real `Worker` loop until no
    `pending` runs remain anywhere in the database (not just ones this
    process claimed -- the other spawned process is racing it for the
    same rows) or `timeout_seconds` elapses, whichever comes first, then
    stops. Bounded by the timeout as a safety net so a stuck run can't
    hang the test suite; the later DB assertions catch that case with a
    clear failure instead."""
    asyncio.run(_drive_until_drained(max_concurrent, poll_interval, timeout_seconds))


async def _drive_until_drained(
    max_concurrent: int, poll_interval: float, timeout_seconds: float
) -> None:
    worker = Worker(max_concurrent=max_concurrent, poll_interval=poll_interval)
    task = asyncio.create_task(worker.run_forever())

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        async with AsyncSessionLocal() as session:
            pending = await session.scalar(
                select(func.count()).select_from(Run).where(Run.status == "pending")
            )
        if pending == 0:
            break
        await asyncio.sleep(poll_interval)

    worker.stop()
    await task


@pytest.fixture
async def pending_runs():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(
            name="concurrent-worker-test", inbox_path="/tmp/concurrent-worker-test-inbox"
        )
        session.add(corpus)
        await session.flush()

        runs = [Run(corpus_id=corpus.id, kind="initial", status="pending") for _ in range(NUM_RUNS)]
        session.add_all(runs)
        await session.commit()
        corpus_id = corpus.id
        run_ids = [run.id for run in runs]

    yield corpus_id, run_ids

    async with AsyncSessionLocal() as session:
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id.in_(run_ids)))
        await session.execute(delete(CostEvent).where(CostEvent.run_id.in_(run_ids)))
        await session.execute(delete(Run).where(Run.id.in_(run_ids)))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


@pytest.mark.integration
async def test_two_worker_processes_claim_each_run_exactly_once(pending_runs):
    _corpus_id, run_ids = pending_runs

    ctx = multiprocessing.get_context("spawn")
    processes = [ctx.Process(target=_worker_process_target, args=(3, 0.1, 20.0)) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)

    for process in processes:
        assert not process.is_alive(), "worker process did not finish in time"
        assert process.exitcode == 0

    async with AsyncSessionLocal() as session:
        runs = (await session.scalars(select(Run).where(Run.id.in_(run_ids)))).all()
        assert {run.id for run in runs} == set(run_ids)
        assert {run.status for run in runs} == {"done"}

        for run_id in run_ids:
            starts = (
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.run_id == run_id, AuditEvent.event_type == "classify_start"
                    )
                )
            ).all()
            assert len(starts) == 1, f"run {run_id} was claimed and started more than once"


@pytest.fixture
async def register_entry_for_lock_test():
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="commit-lock-test", inbox_path="/tmp/commit-lock-test-inbox")
        session.add(corpus)
        await session.flush()

        run_a = Run(corpus_id=corpus.id, kind="update", status="running")
        run_b = Run(corpus_id=corpus.id, kind="update", status="running")
        session.add_all([run_a, run_b])
        await session.flush()

        claim_a = Claim(
            run_id=run_a.id,
            corpus_id=corpus.id,
            subject="Feature X",
            predicate="notes",
            object="note-a",
            confidence=0.9,
        )
        claim_b = Claim(
            run_id=run_b.id,
            corpus_id=corpus.id,
            subject="Feature X",
            predicate="status",
            object="green",
            confidence=0.9,
        )
        session.add_all([claim_a, claim_b])
        await session.flush()

        entry = RegisterEntry(
            corpus_id=corpus.id, feature_key="feature-x", fields={"owner": "alice"}
        )
        session.add(entry)
        await session.commit()

        corpus_id = corpus.id
        run_a_id, run_b_id = run_a.id, run_b.id
        claim_a_id, claim_b_id = claim_a.id, claim_b.id
        entry_id = entry.id

    yield corpus_id, run_a_id, run_b_id, claim_a_id, claim_b_id, entry_id

    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(RegisterFieldSource).where(RegisterFieldSource.register_entry_id == entry_id)
        )
        await session.execute(delete(RegisterEntry).where(RegisterEntry.id == entry_id))
        await session.execute(delete(AuditEvent).where(AuditEvent.run_id.in_([run_a_id, run_b_id])))
        await session.execute(delete(Claim).where(Claim.run_id.in_([run_a_id, run_b_id])))
        await session.execute(delete(Run).where(Run.id.in_([run_a_id, run_b_id])))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


@pytest.mark.integration
async def test_concurrent_commits_same_corpus_do_not_corrupt_register_entries(
    register_entry_for_lock_test,
):
    corpus_id, run_a_id, run_b_id, claim_a_id, claim_b_id, entry_id = register_entry_for_lock_test

    state_a = AgentState(
        run_id=run_a_id,
        corpus_id=corpus_id,
        kind="update",
        trigger_doc_id=None,
        register_diff=RegisterDiff(
            changes=[
                RegisterFieldChange(
                    id=uuid.uuid4(),
                    feature_key="feature-x",
                    field_name="notes",
                    old_value=None,
                    new_value="note-a",
                    claim_id=claim_a_id,
                    status="approved",
                )
            ]
        ),
    )
    state_b = AgentState(
        run_id=run_b_id,
        corpus_id=corpus_id,
        kind="update",
        trigger_doc_id=None,
        register_diff=RegisterDiff(
            changes=[
                RegisterFieldChange(
                    id=uuid.uuid4(),
                    feature_key="feature-x",
                    field_name="status",
                    old_value=None,
                    new_value="green",
                    claim_id=claim_b_id,
                    status="approved",
                )
            ]
        ),
    )

    # Real concurrency at the DB level: each commit_node call opens its own
    # AsyncSessionLocal (its own connection/transaction), so gather here is
    # two genuinely concurrent Postgres transactions racing for the same
    # advisory lock key, not just interleaved Python bytecode.
    await asyncio.gather(commit_node(state_a), commit_node(state_b))

    async with AsyncSessionLocal() as session:
        entry = await session.get(RegisterEntry, entry_id)
        assert entry.version == 3  # base (1) + two applied changes, neither lost
        assert entry.fields["owner"] == "alice"
        assert entry.fields["notes"] == "note-a"
        assert entry.fields["status"] == "green"

        updated_events = (
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.event_type == "register_field_updated",
                    AuditEvent.run_id.in_([run_a_id, run_b_id]),
                )
            )
        ).all()
        assert len(updated_events) == 2

        sources = (
            await session.scalars(
                select(RegisterFieldSource).where(RegisterFieldSource.register_entry_id == entry_id)
            )
        ).all()
        assert {source.field_name for source in sources} == {"notes", "status"}
