# Architecture

## Database

One Postgres 16 + pgvector instance holds state, vectors, audit, and cost
(see CLAUDE.md architectural rules). Schema is managed by Alembic under
`backend/alembic/`; the first migration (`001_initial`) creates every table
from implementation_plan.md section 6.1 by hand-written `op.execute()` SQL,
since the pgvector `hnsw` index and the `pg_trgm` extension aren't
expressible through the ORM.

LangGraph checkpoint tables are created lazily by PostgresSaver on first
use, not by this migration. That lazy `CREATE TABLE IF NOT EXISTS` is not
itself safe under real concurrency -- see `_setup_checkpointer` below.

## Agent graph

`backend/app/agent/graph.py` builds a LangGraph `StateGraph` over
`AgentState` (`backend/app/agent/state.py`) and compiles it with
`AsyncPostgresSaver`, using `runs.id` as the LangGraph `thread_id`.
Resumability (CLAUDE.md behavior 2) comes entirely from that checkpointer:
state is persisted after every node completes, so `run_agent(run_id, ...)`
checks whether a checkpoint already exists for that `thread_id` and, if so,
resumes execution from the last completed node (`ainvoke(None, config)`)
instead of re-running the graph from `START`. A killed-and-restarted
worker calling `run_agent` again for the same `run_id` therefore does not
re-run, and does not re-bill, any stage that already completed.

`AsyncPostgresSaver` connects to the same Postgres instance via `psycopg`
(not `asyncpg`), over its own connection separate from the SQLAlchemy
engine in `db.py` — see `docs/assumptions.md` for why `DATABASE_URL`'s
`+asyncpg` suffix has to be stripped before use.

## Concurrency (CLAUDE.md behavior 9)

**Checkpointer setup under concurrency.** `run_agent`/`resume_run`/
`get_agent_state` each call `AsyncPostgresSaver.setup()` on every
invocation, which runs unguarded `CREATE TABLE IF NOT EXISTS` DDL plus
migration-row inserts. That's harmless when calls are sequential (the old
one-run-at-a-time worker), but two connections calling it at once before
the tables exist race on Postgres's check-then-create semantics for `IF
NOT EXISTS` DDL and can raise `UniqueViolation` on the underlying catalog
row -- exactly what concurrent `run_agent` calls (this step) do the first
time a fresh database is used. `graph.py`'s `_setup_checkpointer` wraps the
call in a session-level `pg_advisory_lock` (not `commit_node`'s
transaction-scoped `pg_advisory_xact_lock` -- the checkpointer's connection
is `autocommit=True`, so a transaction-scoped lock would release between
`setup()`'s individual statements) keyed on a fixed name, serializing
`setup()` across every concurrent caller, in-process or cross-process, and
explicitly unlocking right after so unrelated concurrent runs aren't
serialized by it too.

**Claiming runs.** `backend/app/worker.py`'s `Worker` polls `runs` once a
second and claims up to `settings.MAX_CONCURRENT_RUNS` minus however many
it already has in flight with
`SELECT id FROM runs WHERE status='pending' ORDER BY started_at LIMIT :n
FOR UPDATE SKIP LOCKED`, flipping the claimed rows to `running` in the same
transaction before releasing their locks. Any number of worker processes
can run this loop against the same database: a row another process's
claiming transaction currently holds is simply skipped, not waited on, so
a run is always claimed by exactly one process, never both. Each claimed
run is driven concurrently via `asyncio.create_task(run_agent(...))`, capped
per-process at `MAX_CONCURRENT_RUNS`. See `backend/tests/test_concurrent.py`
(`test_two_worker_processes_claim_each_run_exactly_once`), which spawns two
real OS processes via `multiprocessing` against one Postgres and asserts
every claimed run's audit trail shows it started exactly once.

**Same-corpus register writes.** `agent/nodes/commit.py`'s `commit_node`
acquires a Postgres advisory lock scoped to `corpus_id`
(`pg_advisory_xact_lock(hashtext(corpus_id))`) before writing any
`register_entries` / `register_field_sources` row, whenever it has real
register work to do. This was chosen over wrapping the same code in a
`SERIALIZABLE` transaction for two reasons: it matches CLAUDE.md behavior
9's own wording ("Same-corpus concurrent updates use an advisory lock on
`corpus_id` in the commit node") rather than the alternative TASK_BREAKDOWN
Step 26 also allows, and it fails closed without extra code —
`pg_advisory_xact_lock` blocks the second transaction until the first
commits, so it never needs the client-side retry-on-`40001` loop a
`SERIALIZABLE` transaction would require when two transactions genuinely
conflict. The lock is released automatically at transaction end (commit or
rollback), so `commit_node` never has to unlock explicitly.

Without this lock, two concurrent `commit_node` calls touching the same
`register_entries` row race classically: both read the row's current
`fields`/`version` before either writes, so whichever commits second
silently overwrites the first's change instead of merging with it (a lost
update) — the row ends up missing one of the two changes even though both
were "approved". The lock serializes the two transactions instead: the
second's `SELECT` only runs after the first has committed, so it reads the
already-applied change and its own write lands on top of it, not in place
of it. `backend/tests/test_concurrent.py`
(`test_concurrent_commits_same_corpus_do_not_corrupt_register_entries`)
runs two `commit_node` calls concurrently against the same
`register_entries` row via `asyncio.gather` and asserts both field changes
and both version increments survive.
