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
