# Architecture

## Database

One Postgres 16 + pgvector instance holds state, vectors, audit, and cost
(see CLAUDE.md architectural rules). Schema is managed by Alembic under
`backend/alembic/`; the first migration (`001_initial`) creates every table
from implementation_plan.md section 6.1 by hand-written `op.execute()` SQL,
since the pgvector `hnsw` index and the `pg_trgm` extension aren't
expressible through the ORM.

LangGraph checkpoint tables are created lazily by PostgresSaver on first
use, not by this migration.

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
