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

- **`test_db_ping.py` treats "can open a connection" as its own reachability
  probe, separate from `ping()`.** Since `ping()` never raises, the test
  can't use a try/except around `ping()` itself to decide skip-vs-fail, so it
  opens a throwaway connection first via `engine.connect()` to decide
  whether to skip, then asserts `ping()` returns `True`. Marked
  `pytest.mark.integration`; `asyncio_mode = "auto"` was added to
  `pyproject.toml`'s `[tool.pytest.ini_options]` so async tests don't each
  need an explicit `@pytest.mark.asyncio`.
