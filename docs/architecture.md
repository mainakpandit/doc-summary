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
