# Assumptions

Ambiguous calls made during the build, logged as they happen.

- **`make dev` runs `npm install` in `frontend/` when `node_modules` is missing.**
  The Makefile spec (task_breakdown.md step) didn't list this explicitly, but
  CLAUDE.md behavior 6 requires `make dev` to succeed from a fresh clone, and
  a fresh clone has no `frontend/node_modules`. Guarded so it's a no-op once
  deps are installed.

- **Added `backend/__init__.py`.** Every other package under `backend/`
  (`app`, `app/agent`, `app/api`, `tests`, ...) already had one; `backend/`
  itself didn't. Without it, pytest can't resolve `from backend.app... import`
  in test modules (it has no way to put the repo root on `sys.path`), so
  `backend/tests/test_db_ping.py` failed to collect. Adding the missing
  `__init__.py` matches the existing convention and fixes collection instead
  of reaching for a `pythonpath` pytest-ini workaround.

- **`ping()` swallows its own exceptions and returns `False`.** Keeps
  `services/api/health.py` a one-liner (route handlers must stay thin per
  CLAUDE.md) and keeps the "is the DB up" check in one place. The tradeoff:
  callers can't distinguish "unreachable" from "some other DB error" from the
  return value alone — only the `db_ping_failed` warning log carries that.

- **`DATABASE_URL` is now derived from `POSTGRES_USER`/`POSTGRES_PASSWORD`/
  `POSTGRES_DB` instead of being a separately hardcoded string.** Originally
  `DATABASE_URL` defaulted to `postgres:postgres@.../pm_analyst` while
  `docker-compose.yml` initializes the container from
  `POSTGRES_USER=pm_analyst` (the official Postgres image only creates that
  role, not a separate `postgres` superuser, when `POSTGRES_USER` is set) —
  this made `alembic upgrade head` fail auth against the compose db. Rather
  than just fixing the hardcoded value and leaving two fields that have to
  be kept in sync by convention, `config.py` now has a
  `_default_database_url` model validator that builds `DATABASE_URL` from
  the `POSTGRES_*` fields whenever `DATABASE_URL` itself is left unset, so
  they can't drift apart again. An explicit `DATABASE_URL` (env var or
  `.env`) still wins, for pointing at a non-local database in prod.

- **`test_db_ping.py` treats "can open a connection" as its own reachability
  probe, separate from `ping()`.** Since `ping()` never raises, the test
  can't use a try/except around `ping()` itself to decide skip-vs-fail, so it
  opens a throwaway connection first via `engine.connect()` to decide
  whether to skip, then asserts `ping()` returns `True`. Marked
  `pytest.mark.integration`; `asyncio_mode = "auto"` was added to
  `pyproject.toml`'s `[tool.pytest.ini_options]` so async tests don't each
  need an explicit `@pytest.mark.asyncio`.

- **`parsers.py` MIME detection combines `python-magic` (content sniff) with
  the file extension, letting the extension pick the parser/mime_type and
  using the sniff only to reject a mismatch (e.g. a renamed binary).**
  `python-magic` alone can't distinguish `.md`/`.txt`/`.csv` (all sniff as
  `text/plain`) or tell a `.docx` from any other zip, so content-only
  detection can't drive parser selection the way the prompt implies
  ("Detect MIME via python-magic combined with extension"). Note also that
  `python-magic` wraps the system `libmagic` shared library, which `uv sync`
  does not install — a fresh clone needs `brew install libmagic` (macOS) or
  `apt-get install libmagic1` (Debian/Ubuntu) before `make dev`/`make test`
  will import `backend.app.services.parsers` successfully. This is a gap in
  CLAUDE.md behavior 6 ("easy setup") worth closing (e.g. a `make dev` check
  with a clear error) when ingestion wiring lands, not fixed here since it's
  outside this step's scope.

- **`finding_sources` has no SQLAlchemy model.** Migration 001 creates it
  with no primary key and no unique constraint (`finding_id NOT NULL`,
  `chunk_id` and `claim_id` both nullable, no `PRIMARY KEY` clause) — unlike
  `claim_sources` and `register_field_sources`, which have real composite
  keys. The model spec only calls for `Finding.sources`-style relationships
  where one is actually needed (`Claim.sources`, `RegisterEntry.field_sources`),
  and none was requested for findings. Mapping `finding_sources` as a
  declarative class would require inventing a primary key that doesn't exist
  in the migration, which would violate "match nullability from migration
  001" in one direction or the other. Left unmapped in
  `backend/app/models/finding.py`; write to it with SQLAlchemy Core
  (`sqlalchemy.Table` reflection or a hand-written `Table` + `insert()`) if
  a service ever needs to populate it. Add a composite/surrogate key to a
  new migration first if ORM mapping becomes necessary.

- **`LLMProvider.complete` now takes `stage` as a fifth argument.** The
  Step 13 spec keys `FakeLLM`'s fixture lookup on `(stage, sha256(system +
  json(messages)))`, but `stage` previously stopped at `call_claude` and
  never reached `provider.complete(...)`. Threaded it through the
  `LLMProvider` protocol and `AnthropicProvider.complete` (which accepts
  and ignores it — the real Anthropic API has no use for it) rather than
  giving `FakeLLM` a side channel, so the protocol stays the single
  source of truth for what a provider implementation receives.

- **`backend/app/services/embeddings.py` created early, but only as a
  provider-factory seam.** Step 13 asks `fake_embedder` to monkeypatch
  "the provider factories in services/llm.py and services/embeddings.py",
  but the real embeddings service is Step 14. Added just the
  `EmbedderProvider` protocol and a swappable `_provider_factory` (mirroring
  `services/llm.py`'s pattern; `_default_provider` raises `NotImplementedError`
  pointing at Step 14) so `FakeEmbedder` has something to monkeypatch now.
  `embed_chunks`, batching, retries, and the real Voyage-backed provider are
  unimplemented until Step 14 lands.

- **Added `ruff` and `black` to the `dev` dependency group.** CLAUDE.md
  conventions say "Python: `ruff` + `black`" and `pyproject.toml` already had
  `[tool.ruff]`/`[tool.black]` sections, but neither was actually installed
  (`uv run ruff` failed with "No such file or directory"), so lint had never
  run in this repo. Installing it surfaces a pre-existing `BLE001` ("blind
  except Exception") finding in `test_db_ping.py`'s and `test_ingestion.py`'s
  `_db_reachable()` helpers — `test_llm_wrapper.py` repeats the same idiom
  for its own DB-reachability check, so it now has the same finding. Left
  all three as-is (consistent, pre-existing convention) rather than
  special-casing the new file; a repo-wide lint cleanup is a separate task
  from Step 13.

- **`embed_chunks` takes an explicit `run_id: UUID` parameter, not just
  `(session, chunk_ids)` as literally written in task_breakdown.md Step
  14.** `cost_events.run_id` is `NOT NULL REFERENCES runs(id)` (migration
  001), and CLAUDE.md behavior 10 requires every cost-bearing call to
  attribute its `cost_events` row to a run. Chunks/documents carry no
  `run_id` of their own (only `corpus_id`), so there is no way to derive
  one from `chunk_ids` alone — the caller (an agent node, which always has
  the current run in scope) has to supply it. Matches the existing
  `call_claude(session, run_id, stage, ...)` shape rather than inventing a
  side channel.

- **Embedding provider is Voyage AI's `voyage-3`, called directly over its
  REST API with `httpx` (no `voyageai` SDK added).** Anthropic has no
  native embeddings endpoint, and `voyage-3` was already the default in
  `config.py`/`.env.example` (`EMBEDDING_MODEL`) from an earlier step — kept
  it rather than switching models, and hardcoded the same string as a
  module constant in `embeddings.py` per the task's instruction, mirroring
  `services/llm.py`'s `DEFAULT_MODEL` pattern. Used `httpx` (already a
  dependency) instead of adding the `voyageai` package, matching
  `AnthropicProvider`'s style of a small, direct API client rather than a
  second dependency for a single POST endpoint.

- **`VoyageProvider` does not request a specific `output_dimension` and
  trusts `voyage-3`'s native output width matches
  `chunks.embedding Vector(1536)` / `settings.EMBEDDING_DIM`.** `voyage-3`
  doesn't document a configurable output dimension (unlike
  `voyage-3-large`), so there's no request parameter to force 1536 even if
  the model's real native width differs — that mismatch, if it exists, was
  baked in by the earlier step that fixed `EMBEDDING_DIM=1536` and the
  `Vector(1536)` column, not introduced here. If it turns out `voyage-3`'s
  real output width isn't 1536, the fix is changing `EMBEDDING_MODEL` (or
  adding a migration to resize the column), not something `embeddings.py`
  should paper over silently.

- **Embedding cost is computed from an estimated token count
  (`len(text) // 4`), not a real usage figure from the provider.**
  `EmbedderProvider.embed(texts) -> list[list[float]]` (as specified) has
  no room for a usage/token-count return value, unlike
  `LLMProvider.complete`, which returns real `input_tokens`/`output_tokens`
  from the Anthropic API. The chars/4 heuristic mirrors `services/llm.py`'s
  own pre-call budget estimate, so at least the two cost paths are
  consistent with each other even though the embedding one has no ground
  truth to check against.

- **Added `services.embeddings.embed_query(text) -> list[float]`, a thin
  public wrapper around the module's private `_provider_factory`, for
  `services/retrieval.py` to embed the search query.** `retrieval.py`
  needs the same swappable provider seam `embed_chunks` uses (so
  `fake_embedder`/`FakeEmbedder` cover it too), but `_provider_factory` is
  underscore-prefixed and meant to stay private to `embeddings.py`; a
  one-line public function is cheaper than exporting the seam itself.
  `embed_query` does not write a `cost_events` row — unlike a chunk-embed
  batch, a single query embedding has no `run_id` to attribute cost to (the
  task-breakdown signature for `retrieve` is
  `(session, corpus_id, query, k)`, with no `run_id`), and its cost is
  negligible next to a document batch. Also, per `VoyageProvider.embed`'s
  existing docstring, the real provider always sends
  `input_type="document"`, even for a query — `EmbedderProvider.embed` has
  no `input_type` parameter to plumb a `"query"` value through, and adding
  one is out of scope for this step. Voyage's docs note `input_type` is an
  asymmetry optimization, not a correctness requirement, so this is a
  quality gap (slightly worse ranking), not a broken one; fix by adding
  `input_type` to the `EmbedderProvider` protocol if retrieval quality
  needs it later.

- **`retrieval.retrieve`'s vector search filters out chunks with a NULL
  `embedding`** (`Chunk.embedding.is_not(None)`) rather than letting them
  sort last implicitly. Postgres's default `NULLS LAST` on ascending
  `ORDER BY` would put them last anyway, but relying on that default was
  worth naming explicitly — it's also what makes `test_retrieval.py`'s
  exact-match test valid: its chunks are deliberately never embedded, so
  the vector side is empty and the assertion is purely about the trigram
  side of the fusion.

- **`AsyncPostgresSaver` connects via `psycopg`, not `asyncpg`, so
  `agent/graph.py` strips the `+asyncpg` driver suffix off
  `settings.DATABASE_URL` before handing it to
  `AsyncPostgresSaver.from_conn_string`.** SQLAlchemy's async engine needs
  `postgresql+asyncpg://...`; `langgraph-checkpoint-postgres` opens its own
  connection with `psycopg.AsyncConnection.connect(...)`, which doesn't
  understand the `+asyncpg` suffix and would fail to parse it. Both drivers
  end up pointed at the same single Postgres instance (CLAUDE.md: "one
  database"), just over two separate connections/libraries — LangGraph
  checkpoint tables are not visible through the SQLAlchemy engine or
  `models/`, only through the saver.

- **`agent/graph.py` rebuilds and recompiles the `StateGraph` inside every
  `run_agent` call, instead of compiling once at import time.** Two
  reasons: (1) `AsyncPostgresSaver.from_conn_string` is an async context
  manager tied to one psycopg connection, so a fresh checkpointer (and
  thus a fresh compile) per call avoids holding a connection open for the
  worker's whole lifetime; (2) `build_graph()` looks up `_start_node` /
  `_finish_node` by name from module globals at call time, so
  `test_agent_resume.py` can `monkeypatch.setattr(graph_module,
  "_finish_node", ...)` and have it take effect on the very next
  `run_agent` call, with no need to reload the module or restart a
  process to simulate "the worker crashed and restarted."

- **`run_agent` does not catch exceptions or set `runs.status = 'failed'`.**
  Step 17's instructions only specify the happy-path transition
  (`pending -> running -> done`); a `runs.status` value of `'failed'` and
  any retry/backoff policy around it are unspecified here and read as
  Step 26's concern (concurrent worker reconciliation). Leaving a crashed
  run's status exactly as the caller set it before invoking `run_agent`
  (`worker.py` sets `'running'`) is also more honest: a real `kill -9`
  wouldn't get a chance to run an `except` block either, so this is what
  the state machine would actually look like after a real crash, not an
  idealized one.

- **`test_agent_resume.py`'s "restart the worker" step calls
  `agent.graph.run_agent(...)` directly for the crashed run, rather than
  calling `worker.claim_pending_run()` / `run_once()` a second time.**
  `claim_pending_run()` only selects rows with `status = 'pending'`, and a
  crashed run is left at `status = 'running'` (see above) — so a second
  call to the worker's own pending-run picker would find nothing to do.
  Reconciling orphaned `'running'` rows left behind by a killed worker
  process is Step 26 (`SELECT ... FOR UPDATE SKIP LOCKED`), not this one.
  Calling `run_agent` directly for the same `run_id` is what "the worker
  process restarts and resumes the run it was on" reduces to at this
  step, since `run_id` doubles as the LangGraph `thread_id` and resumption
  is entirely keyed off that, not off `runs.status`.

- **`AgentState`'s nested types (`DocumentRef`, `ClaimDraft`,
  `ConflictDraft`, `FindingDraft`, `RegisterDiff`, `ReviewDecision`,
  `RegisterEntryDraft`, `RegisterFieldChange`, `ClaimSourceDraft`) are
  defined in `agent/state.py` even though implementation_plan.md section 8
  only names them, not their fields.** Shaped each one to match what the
  node that populates it is specified to produce later in the plan:
  `ClaimDraft`/`ClaimSourceDraft` mirror `extract`'s `{subject, predicate,
  object, confidence, sources: [{chunk_id, quote}]}` (8.2/Step 19),
  `ConflictDraft` mirrors `detect_conflicts`'s `(subject, predicate,
  claim_a_id, claim_b_id)` grouping (8.3/Step 20), `FindingDraft` mirrors
  `examine`'s findings (8.4/Step 21), and `RegisterDiff`'s
  `additions`/`changes`/`unaffected` mirror `build_register`/`update`'s
  spec verbatim (8.5/Step 22, Step 30-2). None of this is populated yet —
  only the `start`/`finish` placeholder nodes exist — so treat these
  shapes as a first draft the node implementing each stage is free to
  adjust once it's actually writing to them.

- **`embed_chunks` retries a batch only on `httpx.TransportError`
  (connection failures, timeouts), not on any exception.** The task says
  "retries ... on network errors", not all errors; an HTTP error response
  (bad request, 401) is not transient and retrying it would just waste
  three more calls before failing anyway, so those propagate immediately.
  Nothing is written to the database (no `UPDATE`, no `cost_events` row)
  until every batch in the call has succeeded, so a call that ultimately
  fails after retries leaves the database untouched and is safe to re-run
  in full.

- **`backend/tests/conftest.py` now starts one Postgres+pgvector
  `testcontainers` container unconditionally for every pytest session**
  (via a `pytest_configure` hook, not a fixture), not only when
  `integration`-marked tests are selected. `backend.app.db.engine` is a
  module-level singleton built at import time from
  `get_settings().DATABASE_URL`, and several existing tests
  (`test_db_ping.py`, `test_retrieval.py`, `test_ingestion.py`,
  `test_llm_wrapper.py`, `test_embeddings.py`) import it directly at
  module scope — by the time any fixture could run, those imports would
  already have bound the engine to whatever `DATABASE_URL` was live
  before the fixture executed. Only a hook that runs before collection
  (`pytest_configure`) can win that race, and a hook can't be scoped to
  "only if a later-collected test needs it." Net effect: `make test` now
  requires Docker to be running, and the full suite takes a few extra
  seconds per session to start the container and run
  `alembic upgrade head` against it. `pytest_unconfigure` stops the
  container at session end.

- **`db_session` uses SQLAlchemy's `join_transaction_mode="create_savepoint"`
  recipe** (one outer transaction per test, rolled back in a `finally`;
  each `AsyncSession` built on it gets its own SAVEPOINT) instead of a
  plain `session.begin()` / rollback. Several services call
  `await session.commit()` internally (`embed_chunks`, `call_claude`), and
  a plain nested-session approach would let those inner commits end the
  test's own transaction early, leaking rows to the next test. The
  SAVEPOINT recipe makes an inner `commit()` release-and-reopen the
  savepoint instead, so only the outer `connection.rollback()` in
  `db_session`'s `finally` block actually discards anything.

- **pytest config moved from `[tool.pytest.ini_options]` in
  `pyproject.toml` to a new `pytest.ini` at the repo root**, per this
  task's explicit instruction. Pytest only honors one of the two (whichever
  it finds first, and `pytest.ini` wins), so the old section was deleted
  rather than left as dead, misleading config. Coverage settings
  (`[coverage:run]` / `[coverage:report]`) live in the same `pytest.ini`
  file, wired in via `--cov-config=pytest.ini` in `addopts` — `coverage.py`
  does not read `[coverage:*]` sections out of a file named `pytest.ini`
  on its own, only from `.coveragerc` / `setup.cfg` / `tox.ini` /
  `pyproject.toml`, so that flag is required for the sections to take
  effect at all.

- **`classify` (8.1) sends only `document_id` and `filename` to the model,
  never chunk text.** `DocumentRef` (agent/state.py) carries nothing else,
  and fetching chunk content for this stage would be scope creep the task
  didn't ask for. Filenames are treated as low-risk metadata rather than
  the "source text" CLAUDE.md behavior 8 requires wrapping via
  `injection_guard.wrap_sources` — that module doesn't exist yet as of
  this step (no node has needed it before `classify`). This is a real gap,
  not a permanent design decision: a filename is still attacker-influenced
  input (a corpus contributor picks it), so once `injection_guard` lands
  (expected with `extract`, 8.2, which does send chunk text), revisit
  whether `classify`'s payload should route through `wrap_sources` /
  `scan_response` too instead of being grandfathered out.

- **`classify_review` (8.1) is a stub that only writes a
  `classify_review_noted` audit event, not a real review UI.** Mirrors the
  MVP cut CLAUDE.md already makes explicit for claims/conflicts/findings
  ("Do not add per-claim human review to the UI for MVP. The gate handles
  conflicts, findings, and register changes only.") — extending that same
  cut to low-confidence classifications. The escalation itself is not
  lost: `classify` already writes a `classify_escalated` audit event with
  the offending `document_id`/`doc_type`/`confidence` before routing here,
  so the information is in the audit log for `human_gate` (8.6) or a
  future UI to surface if that scope is ever picked back up.
