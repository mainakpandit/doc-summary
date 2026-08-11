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

- **`backend/app/services/injection_guard.py` (`wrap_sources` /
  `scan_response`) lands with `extract` (8.2), not as a separate step.**
  This is exactly the gap the `classify` (8.1) assumption above flagged:
  `extract` is the first node to send chunk text to an LLM, so it's the
  first node that needs the CLAUDE.md behavior 8 module to exist at all.
  Put it under `services/` (alongside `llm.py`, `embeddings.py`,
  `retrieval.py`) rather than `agent/`, since it's a stateless text
  transform with no dependency on `AgentState` and future nodes besides
  `extract` (e.g. `examine`, 8.4) will plausibly need it too.
  `wrap_sources` takes `list[tuple[uuid.UUID, str]]` rather than `Chunk`
  ORM objects, so the module has no dependency on `models/` and is
  unit-testable without a database. `classify` (8.1) is *not* retrofitted
  to route its filename-only payload through this module in this step --
  that revisit is still open, unchanged from the prior note.

- **`extract` (8.2) calls `injection_guard.scan_response` on every LLM
  response and, on a hit, writes a `possible_prompt_injection` Finding**
  (not just an audit event), even though the task instructions for this
  step only spelled out the quote-verification path. CLAUDE.md behavior 8
  says a scan hit must produce a `possible_prompt_injection` *finding*,
  and the `findings` table already has exactly the shape for it
  (`rule_id`, `severity`, `subject`, `message`, `status`) -- writing an
  audit event instead would technically log the hit but not what the
  behavior names. A hit does not fail the run or reject the claim on its
  own; it's a signal for a human at the gate, matching "never a silent
  side effect" without turning a heuristic smell test into a hard block.

- **`extract` (8.2) drops a claim entirely if *any* of its sources fails
  quote verification, rather than dropping just the bad source and
  keeping the claim with its remaining valid sources.** The task
  instructions say "If not, drop the claim" (singular decision per claim,
  not per source), and this is also the more conservative reading of "no
  bluff": a claim backed by one fabricated citation is not more
  trustworthy just because it also happens to cite one real one.

- **`extract` (8.2) is wired into the graph as `classify -> extract ->
  finish`, with `classify_review` also falling through to `extract`**
  (previously `classify`'s non-escalated branch went straight to
  `finish`, and `classify_review` also went straight to `finish`). Low
  classification confidence is a soft escalation for a human, not a
  reason to skip extraction, so both branches now converge on `extract`
  before `finish`. This changes `test_agent_resume.py`'s node-completion
  count for an empty-documents run from 2 (`classify`, `finish`) to 3
  (`classify`, `extract`, `finish`) -- updated in the same commit rather
  than left to fail, since behavior 2 (resumable) is the thing that test
  exists to protect and the count itself, not the resume logic, is what
  changed.

- **`extract` (8.2) sends the *entire* set of a document's chunks to
  Claude in one `call_claude` invocation per document**, not a
  `retrieval.retrieve`-style top-k query. The task says "for each
  document, retrieve its chunks" (all of them, in document order by
  `idx`) -- `retrieval.retrieve` is a similarity search keyed on a query
  string, which doesn't fit "extract every claim from this whole
  document." No batching/splitting across multiple calls for large
  documents is added here; that's a cost/context-limit concern out of
  this step's scope.

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

- **`detect_conflicts` (8.3) only creates a `conflicts` row for a pair of
  claims whose `object` actually differs, not for every pair within a
  qualifying `(subject, predicate)` group.** A group with 3+ claims can
  have >1 distinct object while still containing same-object pairs (e.g.
  objects `[A, A, B]`); pairing all `C(n, 2)` combinations would record a
  row for the `(A, A)` pair even though those two claims agree, which
  isn't a conflict by any reading of the term. The self-join instead
  requires `a.object <> b.object` in addition to `a.id < b.id` (the
  latter just avoids emitting both orderings of the same pair).
  `resolution` starts `'unresolved'` per the task spec; no de-duplication
  beyond that is attempted (e.g. collapsing transitive conflicts across
  3+ distinct objects into fewer rows) since the task only specifies
  per-pair rows.

- **`detect_conflicts` (8.3)'s single SQL statement avoids `COUNT(DISTINCT
  object) OVER (PARTITION BY subject, predicate)`** — Postgres rejects
  `DISTINCT` inside a window function call outright. `DENSE_RANK() OVER
  (PARTITION BY subject, predicate ORDER BY object)` is used instead:
  claims sharing an `object` get the same rank within their group, so
  `MAX(that rank) OVER (PARTITION BY subject, predicate)` in a second CTE
  layered on top is the distinct-object count without needing `DISTINCT`
  inside a window frame at all. Window functions can't nest in a single
  `SELECT`, which is why this is two stacked CTEs (`ranked`, then
  `scored`) rather than one.

- **`detect_conflicts` (8.3) queries `claims` for the current `run_id`
  directly from the database, not `state.claims`.** Matches `extract`
  (8.2) re-fetching `chunks` from the DB rather than trusting in-memory
  state, and keeps the node correct on resume: if the graph is killed and
  restarted between `extract` and `detect_conflicts`, the checkpointed
  `AgentState` should already carry the persisted claims either way, but
  querying the table directly removes any dependency on that being true.

- **Rules playbook (8.4) deterministic DSL uses structured YAML fields
  (`op`/`predicate`/`value`), not a literal `every_subject_has(owner)`
  function-call string.** The task's prose describes the three predicate
  verbs with call syntax, but parsing that out of a YAML scalar (handling
  quoting, values containing parens, etc.) buys nothing a
  `Literal["every_subject_has", "no_subject_has", "at_least_one"]` field
  doesn't already give for free via pydantic validation. `backend/app/agent/rules/schema.md`
  documents the mapping between the two notations explicitly so the
  function-call shorthand in this file's own prose still reads correctly.

- **The third starter rule (`shipped_requires_release_notes`) uses the
  `llm` evaluator, not `deterministic`.** "No feature has status=shipped
  without a linked release-notes source" needs to know which *document*
  backs a claim, not just its `(subject, predicate, object)` triple — the
  three-verb predicate DSL only ever looks at claim fields, so it
  structurally cannot express this rule. `examine_node` resolves each
  claim's backing document `doc_type`s via `claim_sources -> chunks ->
  documents` and hands that (plus the claims' verbatim quotes, wrapped
  per CLAUDE.md behavior 8) to `call_claude(stage="examine")`.

- **`finding_sources` (still unmapped as a declarative class, per the
  existing note above) is now populated via a bare `sqlalchemy.Table`
  sharing `Base.metadata`, defined in `models/finding.py` next to
  `Finding` rather than in `services/rules.py` or `agent/nodes/examine.py`.**
  Keeps every table's shape in `models/`, Core or declarative, in one
  place; `examine.py` imports `finding_sources` and writes to it with
  `insert()`, same as any other model.

- **`examine` (8.4) writes an `examine_clean` audit event whenever zero
  rules produced a violation, including when the corpus has no
  `rules_path` at all (zero rules to evaluate).** "Nothing was wrong" is
  trivially true with nothing to check, and it keeps the event's meaning
  uniform — "examine ran and every rule that existed passed" — rather
  than requiring callers to distinguish "ran with zero rules" from "ran
  and passed." A `possible_prompt_injection` finding from a `scan_response`
  hit on an `llm`-rule's response does not suppress `examine_clean` (it
  isn't a rule violation), matching how `extract`'s own injection finding
  doesn't touch that node's claim-acceptance logic either.

- **Wiring `examine` into `graph.py` (between `detect_conflicts` and
  `finish`) required updating `test_agent_resume.py`'s fixture and
  assertions**, discovered by running the full suite after this step: the
  fixture's `pending_run` teardown deleted `Run` before `AuditEvent`,
  which started raising a `ForeignKeyViolationError` once `examine` began
  writing an `examine_clean` audit event for that test's (rules-less)
  corpus on every run — teardown now deletes `AuditEvent` first. The test
  also asserts an exact count of LangGraph checkpoint "node completions"
  for a full graph run, which necessarily went from 4 to 5 with a new
  node in the path.

- **`build_register` (8.5, initial-run path) never writes to
  `register_entries` / `register_field_sources`, even though the task
  description's "Insert register_entries and register_field_sources rows"
  read alone suggests it should.** The same paragraph immediately says
  "Do not commit yet; this is the 'proposed' state that the human gate
  will confirm" and "Store the proposal in `state.register_diff` as
  `additions` only" — and `register_entries` has no `status`/`resolution`
  column the way `findings`/`conflicts` do, so there is no way to mark a
  row "proposed, not yet approved" the way those tables can. Writing a
  live row before a human confirms it would make the proposal
  indistinguishable from a committed one, contradicting "Humans gate
  every commit" and CLAUDE.md behavior 9's mention of a dedicated
  `commit` node (advisory-locked per `corpus_id`) as where mutations
  land. Read the two instructions together as: this node computes what
  *would* be inserted and hands it to the graph via
  `state.register_diff.additions`; the literal `INSERT` is the
  not-yet-built `commit` node's job, once `human_gate` (8.6) approves.
  Each `RegisterEntryDraft.fields["sources"]` entry carries `field`,
  `claim_id`, `chunk_id`, and `quote` specifically so that future `commit`
  node has everything it needs to populate `register_field_sources`
  (`field_name`, `claim_id` pairs) without re-deriving the winning claim
  per field.

- **`build_register` treats the claim predicate `"risk"` as the source of
  `fields["open_risks"]`**, collecting every matching claim's `object`
  (deduplicated, highest-confidence first) rather than picking one
  winner the way `owner`/`target_release`/`status` do. No predicate
  vocabulary for risks exists yet in `corpus/demo/rules.yaml` or any
  `extract_*.txt` prompt, so this is this node's own naming choice, not
  an existing contract — revisit if a rules playbook or extract prompt
  later standardizes on a different predicate name (e.g. `"open_risk"` or
  `"risks"`) for the same concept.

- **`build_register`'s `"name"` field is the `subject` text of whichever
  claim in the feature's group wins the same highest-confidence/most-
  recent-`ingested_at` selection used for the other fields**, not a
  claim with a literal `predicate == "name"` (no such predicate is
  produced by extraction — a claim's `subject` already *is* the feature's
  name). Two claims that slugify to the same `feature_key` can carry
  slightly different `subject` casing/spelling ("Feature A" vs. "feature
  a"); this picks the most-trusted one as canonical rather than the
  first one seen.

- **`build_register` queries `claims` for the whole `corpus_id` from the
  database, not `state.claims`.** Matches the same call made for
  `detect_conflicts` (8.3) above, and the task explicitly says "group all
  persisted claims for the corpus" — for an `initial` run this is
  equivalent to `state.claims` (nothing else could have populated
  `claims` for a corpus before its first run), but querying directly
  keeps the node correct if that assumption ever stops holding and
  removes any dependency on `state.claims` staying in sync with what's
  actually persisted.

- **`build_register` no-ops for `kind == "update"` runs**, writing a
  `build_register_skipped` audit event instead of computing anything.
  The plan lists `build_register` and `update` as two separate 8.5
  pieces and this task's instructions only specify the initial-run
  grouping behavior; incremental register updates (diffing against
  existing `register_entries` rows rather than proposing fresh ones) are
  a distinct, not-yet-built piece of work, matching how `classify_review`
  (8.1) stubs out its own out-of-scope corner instead of silently doing
  nothing.

- **Wiring `build_register` into `graph.py` (between `examine` and
  `finish`) required bumping `test_agent_resume.py`'s expected
  checkpoint "node completions" count from 5 to 6**, same mechanical
  update as `examine`'s own wiring above, for the same reason: one more
  node now sits on the path every run takes before `finish`.

- **`injection_guard.scan_response` now returns `list[Smell]`** (a
  `NamedTuple` of `category` + `excerpt`) instead of `list[str]` of raw
  matched pattern text, per this step's explicit `scan_response(text) ->
  list[Smell]` signature. `services/rules.py`'s `EvaluationResult
  .injection_smells` and `_evaluate_llm`'s return type follow suit;
  `extract.py`'s and `examine.py`'s Finding messages now cite
  `smell.category`/`smell.excerpt` instead of interpolating the raw list.
  `_SMELL_RULES` covers the five categories this step names (`ignore
  previous`, `disregard instructions`, fetch-URL requests, email/send-data
  requests, tool-behavior-change suggestions) plus the three heuristics
  already present before this step (system-prompt probes, role overrides,
  "don't tell the reviewer") — kept rather than removed, since they cost
  nothing and catch smells the five named categories don't.

- **Both `extract.py` and `services/rules.py`'s `_evaluate_llm` now drop
  the LLM response whole on a `scan_response` hit**, instead of writing
  the `possible_prompt_injection` Finding and then still parsing
  claims/verdicts out of the same response. The prior behavior (present
  since `extract` first landed in Step 19) logged the smell but let a
  possibly-hijacked response's content through anyway, which contradicts
  this step's explicit instruction ("on a hit, drop the response ... and
  skip that document"). `extract.py` does this with a `continue` before
  `_parse_response`; `_evaluate_llm` returns `([], smells)` before calling
  `_parse_llm_verdicts`. Neither fails the run: `extract`'s loop moves on
  to the next document, and `examine`'s loop moves on to the next rule.

- **`wrap_sources` now escapes any literal `<untrusted_source>` /
  `</untrusted_source>` tag-shaped text found inside a chunk's own
  content** (HTML-entity-style, `<` -> `&lt;`, `>` -> `&gt;`) before
  wrapping it, so a chunk can't forge a fake closing tag to make text
  after it look like it sits outside the untrusted block. This is what
  this step's "refuse to include any content outside those tags" reads as
  literally: without it, a chunk containing a hand-crafted
  `</untrusted_source>` substring could produce output where the *visual*
  tag boundary no longer matches the *actual* one `wrap_sources` intended,
  even though structurally everything is still inside the one block it
  emitted for that chunk.

- **Every graph node is now wrapped with `agent/instrumentation.py`'s
  `instrument()` at `build_graph()` time, rather than each node file
  writing its own entry/exit audit event.** CLAUDE.md behavior 1 ("every
  agent node emits SSE + audit events on entry and exit") wasn't actually
  implemented before this step -- nodes only had `structlog` entry/exit
  log lines, no `audit_events` rows and no SSE. A wrapper applied once in
  `build_graph()` was chosen over touching all six node files because it
  keeps each node's own internal audit writes (`classify_escalated`,
  `register_entry_proposed`, ...) about *what it decided*, separate from
  *whether it's running*, and because `build_graph()` already re-resolves
  node functions from module globals on every call (for
  `test_agent_resume.py`'s monkeypatching), so wrapping there doesn't
  disturb that. Event names are `<node>_start` / `<node>_end` (e.g.
  `classify_start`, `extract_start`); `run_agent()` also emits a
  `run_completed` event right before flipping `runs.status` to `'done'`,
  which `GET /runs/{id}/events` (task_breakdown Step 24) treats as the
  terminal marker to stop streaming on. A run that crashes emits no
  terminal event, matching `run_agent`'s existing "status stays whatever
  the caller set" failure contract.

- **`POST /runs` does not populate `AgentState.documents` from the
  corpus's ingested documents.** Nothing in the codebase currently does
  this -- `run_agent()` always starts a fresh run with `documents=[]`;
  every existing node test builds `AgentState` by hand with real
  `DocumentRef`s rather than going through `run_agent`. Wiring "which
  documents does an initial vs. update run process" is a real design
  question (all of a corpus's documents? just ones without a prior
  successful extract?) that this step's task text doesn't specify, so
  `classify`/`extract` currently run as structural no-ops (they still
  execute and emit their `_start`/`_end` events) until a later step wires
  this. `test_api_runs.py`'s `test_ingest_then_run_flow` documents this
  gap directly: it asserts `total_usd_cost == 0.0` after a full
  ingest-then-run flow, since no LLM call ever happens without documents
  in scope.

- **`runs.idempotency_key` is enforced globally unique by migration 001**
  (not scoped per corpus), even though task_breakdown Step 24 describes
  `POST /runs`'s `Idempotency-Key` semantics as "if seen for the same
  corpus, return the existing run." `services/runs.py`'s `create_run`
  looks the key up globally; if it's already attached to a run for a
  *different* `corpus_id`, that's an `IdempotencyKeyConflict` (mapped to
  HTTP 409) rather than either silently returning the wrong run or
  hitting a raw `IntegrityError` from the unique constraint. The normal
  case -- a client retrying the exact same request -- always pairs the
  same key with the same corpus_id, so the 409 path only fires on a
  client bug (reusing a key across genuinely different requests).

- **`services.ingestion.save_upload` names uploaded files by content
  hash** (`CORPUS_ROOT/<corpus_id>/<sha256>.<ext>`), which means
  `Document.filename` (set by `ingest_file` to `path.name`) is the hash,
  not the client's original filename. Task_breakdown Step 24 specifies
  this exact save path, and `ingest_file` (Step 11) already sets
  `filename = path.name` for every caller, so preserving the original
  upload filename would mean either changing `ingest_file`'s established
  behavior (used by the watcher and tests too) or adding an
  `original_filename` column that doesn't exist in migration 001. Neither
  is in scope here; the original filename is simply not retained.

- **`GET /runs/{id}`'s `current_stage`** is derived from the most recent
  `audit_events` row (for that run) whose `event_type` ends in `_start`
  or `_end`, scanning newest-first past any of a node's own
  business-detail events (e.g. `register_entry_proposed` sits between
  `build_register_start` and `build_register_end`). It does not
  distinguish "mid-flight" from "just finished" (both a fresh `_start` and
  the matching `_end` report the same stage name) -- CLAUDE.md just asks
  for "current stage plus counts," and the full detail is already
  available via `GET /runs/{id}/audit`.

- **A conflict's approve/reject decision at the human gate maps to
  `resolution='kept_both'` / `'rejected_both'`**, not `'kept_a'`/`'kept_b'`.
  `POST /runs/{id}/review`'s contract (task_breakdown Step 25) is a binary
  `decision: "approve"|"reject"` per item, and IMPLEMENTATION_PLAN.md 10.3
  describes the review UI the same way ("an approve button, a reject
  button") for every item kind, conflicts included -- there's no UI or API
  surface for picking claim A over claim B specifically. Given only two
  choices, "approve" reads as "acknowledge both claims stand despite the
  disagreement" (`kept_both`) and "reject" as "neither assertion is trusted
  going forward" (`rejected_both`), the two `conflicts.resolution` values
  that don't require picking a winner. Note this doesn't feed back into
  `build_register`'s field selection (it already ran, oblivious to
  conflicts, before `human_gate`) -- resolving a conflict here records the
  human's call but doesn't change which claim a given run's register fields
  came from. Closing that gap is future work, not part of this step.

- **`POST /runs/{id}/review`'s `reviewer` comes from the request body**
  (`{items: [...], reviewer: string}`), as task_breakdown Step 25's prompt
  specifies verbatim, rather than the `X-Reviewer` header CLAUDE.md's "Do
  not add auth" section names as the repo's general reviewer-identity
  convention. Read literally, "reviewer identity comes from the X-Reviewer
  header" governs endpoints that need to know who's calling without an
  explicit field for it; this is the first (and so far only) endpoint whose
  request schema already carries an explicit `reviewer` as a piece of
  decision data (whose call this was, stored on `reviews.reviewer` and
  copied onto `conflicts.resolved_by` / `findings.reviewer`), not as
  authentication -- there's still no check that the caller actually is that
  reviewer, so "do not add auth" holds either way. If a later step adds
  more human-gate-adjacent endpoints without their own reviewer field,
  those should fall back to `X-Reviewer`.

- **`POST /runs/{id}/resume` drives the graph resume synchronously inside
  the request** (`agent/graph.resume_run`), rather than flipping
  `runs.status` back to `'pending'` for a worker to pick up, which is what
  IMPLEMENTATION_PLAN.md section 7.10 sketches ("the API layer flips a run
  back to pending with a resume token; the worker picks it up"). `worker.py`
  (task_breakdown Step 26, not yet built) is the only thing that polls for
  `'pending'` runs, and even then only for genuinely new work via
  `run_agent`, which starts a fresh `AgentState` when the checkpoint is
  empty -- it has no path today for "resume a specific interrupted thread
  with `Command(resume=...)`." Driving it synchronously from the route
  mirrors how `run_once()`/`run_agent()` are already invoked directly
  elsewhere in this codebase (e.g. `test_api_runs.py`) and keeps behavior 4
  intact (MCP's future `resume_run` tool calls the same `agent.graph.resume_run`).
  Revisit once Step 26 gives the worker a real polling story for
  `awaiting_review` -> `pending` transitions.

- **`agent/nodes/commit.py`'s `RegisterFieldChange` (update-run field
  change) handling is implemented but unexercised.** `state.register_diff.changes`
  is always `[]` today -- only `build_register` (initial runs) populates
  `register_diff`, and it only ever writes `additions`; the `update` node
  (task_breakdown Step 29) that would populate `changes` doesn't exist yet.
  Implemented anyway rather than left as a gap, since `RegisterFieldChange`
  already flows through `agent/state.py`, `services/review.py`, and
  `human_gate_node` end to end -- silently dropping it at the one place
  that actually writes to `register_entries` would be a worse inconsistency
  than a few untested lines. No test exercises this path directly; it will
  be covered once Step 29's `update` node and `test_incremental.py` land.

- **Added `GET /corpora/{corpus_id}/documents/{document_id}/text`**, not in
  IMPLEMENTATION_PLAN.md section 9's API surface list. `frontend/src/components/SourceViewer.tsx`
  (task_breakdown Step 30) needs a document's full text to highlight the
  exact `[char_start, char_end]` a citation points to, and nothing else in
  the API returns that -- `ingest_file` never persists the whole-document
  string, only per-chunk slices (`Chunk.text`). The new route re-parses the
  file `save_upload` already wrote to disk (`services/ingestion.get_document_text`,
  a thin wrapper around the existing `parsers.parse_file`), which is exactly
  the same parse `ingest_file` ran originally, so the returned text's
  offsets stay consistent with `Chunk.char_start`/`char_end`. Scoped under
  `/corpora/{corpus_id}/documents/...` to match `documents.py`'s existing
  upload route rather than a flat `/documents/{id}/text`, and 404s if the
  document doesn't belong to that corpus.

- **`ReviewGate.tsx`'s "Submit decisions" sends `reviewer` in the POST
  body, not as a bare `X-Reviewer` header**, even though the nav input that
  captures it (task_breakdown Step 30's own assumption line) is labeled
  `X-Reviewer` and the request also carries that header. This follows the
  precedent already logged above for `POST /runs/{id}/review`: the actual
  `ReviewSubmission` schema requires `reviewer` as an explicit body field
  (it's stored on `reviews.reviewer` and copied onto `conflicts.resolved_by`
  / `findings.reviewer`), so a header-only value would 422. The header is
  still sent alongside the body for any future middleware that reads it,
  but the body field is what the route actually consumes.

- **Added `GET /corpora/{id}/register` (`api/register.py`,
  `services/register.py`, `schemas/register.py`).** The Register page task
  named this URL as something to fetch, but no route or service for it
  existed yet -- only `register_entries`/`register_field_sources` tables and
  `commit_node`, which write them. Built it the same way every other
  resource here is: a thin route (404s on unknown `corpus_id`) delegating to
  a service that, per committed entry, groups `register_field_sources` by
  `field_name` and resolves each backing claim's predicate/object/confidence
  plus its citations. Also extracted `services/review.py`'s private
  `_citation`/`_claim_citations` into a new `services/citations.py`
  (`build_citation`/`claim_citations`) so both services -- and the
  frontend's single `Citation` type -- agree on exactly one citation shape,
  rather than the register service growing its own second copy.

- **The Register page's "Sources" column is the union of every citation
  across all of an entry's fields, not a literal `fields.sources`
  column** (no such field exists -- `sources` is stripped out of `fields`
  before `register_entries` is written, see `commit.py`). Its info-icon
  popover lists every distinct backing claim for the row (deduped by
  `claim_id`), so it doubles as a "show everything behind this feature"
  view. This does mean a claim already visible under, say, Owner also shows
  up in Sources' popover -- accepted as a reasonable redundancy rather than
  omitting the union column the task asked for by name.

- **`Register.tsx` resolves its `corpus_id` from a `?corpus=` query param
  (defaulting to the first corpus returned by `listCorpora`), not a route
  param.** The task asked for the page to be wired into the top nav as a
  single link, but nothing in the frontend today lets a user pick a corpus
  before landing on a corpus-scoped page -- `RunsList`/`RunDetail` sidestep
  this by not being corpus-scoped at all. Rather than inventing a separate
  corpus-picker page the task didn't ask for, `/register` carries an
  in-page `<select>` that updates the query param, so the route stays a
  single stable nav target while still being deep-linkable/shareable per
  corpus.
