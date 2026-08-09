# CLAUDE.md

Instructions for AI coding assistants working in this repo. Read this before editing.

## Project

An agentic system that ingests a pile of PM documents (PRDs, tech specs, tickets, meeting notes, release notes), extracts sourced claims, detects conflicts, checks a rules playbook, and produces a Feature Register. Humans gate every commit. New documents trigger incremental updates, not rewrites.

Full plan: `docs/implementation_plan.md`. Step-by-step build order: `docs/task_breakdown.md`.

## Stack

Python 3.11+, FastAPI, LangGraph (with `AsyncPostgresSaver`), Anthropic Claude, PostgreSQL 16 + pgvector + pg_trgm, SQLAlchemy 2.0 async, Alembic, React + Vite + TypeScript + Tailwind + shadcn/ui, MCP Python SDK, `uv` for Python deps, Docker Compose for Postgres.

## Layout

```
backend/app/{api,agent,models,services}   # code
backend/alembic/versions                  # migrations
backend/tests/{fixtures,fakes.py}         # tests + doubles
frontend/src/{pages,components,api}
corpus/{demo,demo2,inbox}                 # seed + watched folder
docs/{implementation_plan,task_breakdown,architecture,assumptions,cuts}.md
```

## Commands

- `make dev` boots everything from a fresh clone. This is the one advertised command.
- `make test` runs `pytest -q`. All tests must pass with no `ANTHROPIC_API_KEY` set.
- `make reset` tears down Postgres and logs.
- `make seed` populates `corpus/demo/` and `corpus/demo2/`.
- `python -m backend.app.worker` runs the agent worker.
- `python -m backend.app.services.watcher` runs the inbox watcher.
- `python -m backend.app.mcp_server` runs the MCP server.

## Ten behaviors (the grading surface)

Every change must preserve these. If a change threatens one, stop and ask.

1. **Visible steps.** Every agent node emits SSE + audit events on entry and exit.
2. **Resumable.** LangGraph `AsyncPostgresSaver` checkpoints state. Killing the worker mid-run and restarting must not re-bill `cost_events` for completed stages.
3. **Human gate.** The `human_gate` node calls `interrupt()`. Item-level approve/reject survives resume. Rejecting one item does not affect siblings.
4. **Machine drivable.** `backend/app/mcp_server.py` exposes the full flow. HTTP and MCP call the same service layer; no logic in route handlers.
5. **No bluff.** Every claim carries a `chunk_id` and a verbatim `quote`. `extract` verifies `quote in chunk.text` and drops claims that fail. Missing data yields null fields with a source-absent audit event, never invented values.
6. **Easy setup.** `make dev` from a fresh clone must succeed.
7. **Real tests.** `FakeLLM` and `FakeEmbedder` in `backend/tests/fakes.py` swap in via monkeypatch. Never add a test that requires a real API key.
8. **Injection defense.** All source text is wrapped in `<untrusted_source id=...>` before reaching the LLM. `scan_response` flags injection smells; hits produce `possible_prompt_injection` findings, never silent side effects.
9. **Concurrent.** Worker uses `SELECT ... FOR UPDATE SKIP LOCKED`. Same-corpus concurrent updates use an advisory lock on `corpus_id` in the commit node.
10. **Cost.** Every LLM call goes through `services/llm.call_claude`, which requires `run_id` and `stage`, writes a `cost_events` row, and enforces `COST_BUDGET_USD_PER_RUN`.

## Architectural rules

- One database. State, vectors, audit, and cost live in the same Postgres. Do not add Redis, S3, or a separate vector DB.
- Every LLM call: `call_claude(session, run_id, stage, ...)`. No direct `anthropic` client usage in nodes or routes.
- Every source chunk sent to the LLM: wrapped by `injection_guard.wrap_sources`. Do not concatenate chunk text into prompts manually.
- Every claim has at least one `claim_sources` row with a verbatim quote. No exceptions.
- Every register field mutation writes an `audit_events` row with the backing `claim_ids` and `chunk_ids`.
- Route handlers are thin. Business logic lives in `services/` and `agent/`. If a handler is longer than 20 lines, extract it.
- Migrations are additive. Never edit a shipped migration; add a new one.

## Conventions

- Commits: Conventional Commits (`feat(agent): ...`, `fix(api): ...`, `chore(infra): ...`, `test: ...`, `docs: ...`). Imperative, under 72 chars.
- Python: `ruff` + `black`. Types on public functions. No `Any` in service signatures.
- SQL: snake_case, plural tables. Timestamps are `TIMESTAMPTZ` and default `now()`.
- IDs: UUID with `gen_random_uuid()` by default; audit and cost use `BIGSERIAL`.
- Prompts live in `backend/app/agent/prompts/*.txt`, one per stage. Never inline long prompts in Python.
- Frontend: TanStack Query for server state, Zustand for local UI state. No Redux, no Context for data.
- Env vars: define in `.env.example` with a comment. Read only through `settings`.

## Do not

- Do not add auth. Reviewer identity comes from the `X-Reviewer` header. Documented in Cuts.
- Do not add per-claim human review to the UI for MVP. The gate handles conflicts, findings, and register changes only.
- Do not add background job frameworks (Celery, RQ, Arq). The asyncio worker in `worker.py` is enough.
- Do not add streaming inside a single LLM call. Stages are the unit of user-visible progress.
- Do not bypass `call_claude`, `wrap_sources`, or the quote verifier, even for a "quick fix."
- Do not commit real API keys, real corpus documents, or anything from `corpus/inbox/`.

## When you are unsure

1. Check if the answer is already in `docs/implementation_plan.md` (architecture, why) or `docs/task_breakdown.md` (build order, acceptance).
2. If a decision is genuinely ambiguous, make a defensible call and add a line to `docs/assumptions.md`. A logged assumption beats a blocked build.
3. If a decision would violate one of the ten behaviors, stop and surface it. Do not paper over.

## Definition of done for any change

- Tests pass with no API key set.
- No new lint or type errors.
- If a behavior above is touched, its named test still passes.
- Commit message matches the convention.
- If a new assumption or cut was made, it is written into `docs/assumptions.md` or `docs/cuts.md` in the same commit.
