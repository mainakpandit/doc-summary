# Writeup

## Architecture, and why

Five long-running pieces, one Postgres:

1. **FastAPI backend** — HTTP + Server-Sent Events for live run progress.
2. **LangGraph agent worker** (`backend/app/worker.py`) — the actual
   thinking, checkpointed to Postgres via `AsyncPostgresSaver`.
3. **PostgreSQL 16 + pgvector + pg_trgm** — state, documents, vectors,
   audit log, cost log, and LangGraph checkpoints, all in one database.
4. **Inbox watcher** (`backend/app/services/watcher.py`) — turns a new file
   dropped in `corpus/inbox/` into an `update` run.
5. **MCP server** (`backend/app/mcp_server.py`) — the same flow, exposed to
   a machine over stdio instead of HTTP.

Plus a React/Vite frontend for the human review gate.

**One database, not three.** Splitting state, vectors, and audit across
separate stores (a queue, a vector DB, a log sink) buys an MVP nothing and
costs transactional integrity — the property that makes "every register
field mutation writes an audit event with the backing claim/chunk ids" a
fact you can prove with one transaction, not an eventually-consistent hope
across two systems. Postgres with pgvector handles vector search well
enough for a demo-scale corpus; sharding is a real, later problem, not a
day-one one.

**LangGraph, not a hand-rolled state machine.** The brief's two hardest
behaviors — resumability and a human gate that pauses mid-flow — are
LangGraph's `AsyncPostgresSaver` and `interrupt()` as first-class
primitives. Writing checkpoint/replay semantics by hand is the kind of
code that looks done and isn't; `test_agent_resume.py` (kill a run
mid-graph, restart, assert it finishes without re-running or re-billing
completed nodes) exercises the exact thing that's easy to get subtly wrong.

**A graph, not a single big LLM call.** Every node — `classify`, `extract`,
`detect_conflicts`, `examine`, `build_register`/`update`, `human_gate`,
`commit` — is a separate, individually observable, individually retryable
unit. That's what makes "visible steps" (behavior 1) a real property
instead of a progress bar animating over one opaque call: every node emits
an SSE + audit event on entry and exit, and a client watching mid-run sees
*which* stage is running, not just "working...".

**Hybrid retrieval (vector + pg_trgm via RRF), not vector-only.** PM
documents are full of exact proper nouns — feature names, ticket ids,
release tags — that a pure embedding search sometimes mis-ranks against a
paraphrase. Trigram keyword search catches the exact match; the vector
side is there for real paraphrase recall once embeddings are actually
wired into ingestion (see Cuts — this build's honest gap).

**Every claim carries a `chunk_id` and a verbatim `quote`, checked, not
trusted.** `extract_node` verifies `quote in chunk.text` before persisting
anything; a claim that fails is dropped whole, not partially kept, and the
drop itself is an audit event. This is the single most load-bearing rule
in the codebase — a Feature Register that occasionally invents an owner is
worse than one with an honest blank, because the blank is visibly
untrustworthy and the invention isn't.

## What the choices cost and buy

| Choice | Buys | Costs |
|---|---|---|
| One Postgres for everything | Transactional integrity across state/audit/register writes; one thing to run, back up, and reason about | A single point of write contention; vector search at real scale eventually wants a dedicated index service |
| LangGraph + Postgres checkpointer | Resumability and human-in-the-loop pausing essentially for free, correctly | A second connection type (`psycopg`, not `asyncpg`) to the same DB, and checkpoint-table setup that needs its own concurrency guard (`_setup_checkpointer`'s advisory lock — see `docs/architecture.md`) |
| Per-node LLM calls, no streaming inside a call | Every stage is a clean, cost-attributable, individually-billed unit; simple to reason about failure per node | More total round-trips than one giant call would need; per-call latency adds up linearly with corpus size |
| Hybrid retrieval via RRF | Exact-name recall today, paraphrase recall once embeddings are wired in | RRF's fusion score isn't a good "is this actually related" signal on a small corpus (see `agent/nodes/update.py`'s docstring on why it isn't used there) — has to be swapped for a raw similarity floor when the question is "should I even look at this document," not "how should I rank documents I've already decided to look at" |
| Quote verification on every claim | No-bluff is a provable property, not a prompt instruction hoped-for | A model that's *right* but phrases its quote slightly differently than the source text still gets dropped — precision over recall, deliberately |
| SKIP LOCKED polling worker, no Celery/Redis | One process type to run, `asyncio`-native, no new infra | A 1-second poll latency floor on picking up new runs; a real task queue would notify instead of poll |

**Money:** every LLM/embedding call is metered through `services/llm.py`'s
`call_claude` / `services/embeddings.py`'s `embed_chunks`, both of which
write a `cost_events` row before anything else happens with the response
and enforce `COST_BUDGET_USD_PER_RUN` up front (`BudgetExceeded` fails the
run rather than silently overspending). `GET /runs/{id}/cost` sums exactly
to its own stage breakdown (`test_cost.py`), so "what did this run cost"
is always an honest, self-consistent number, not an estimate.

**Latency:** a run's wall-clock time is dominated by its number of LLM
calls (one classify call per 3-document batch, one extract call per
document, occasional `examine` calls for `llm`-evaluator rules), not by
Postgres — every DB-only stage (`detect_conflicts`, `commit`) is a single
SQL statement. An `update` run is deliberately cheap relative to an
`initial` one: it only ever processes the triggering document plus a
capped, similarity-filtered set of neighbors (at most three), not the
whole corpus.

**Simplicity:** the biggest simplicity win in this build is that the HTTP
API, the MCP server, and (implicitly) the watcher all call the exact same
`services/*` functions — `create_run`, `submit_review_decisions`,
`list_register_entries`, and so on. There is exactly one implementation of
"what does starting a run mean," not three that have to be kept in sync.
`test_mcp.py` proves this directly: drive a full run through MCP tools
alone, and its register matches the same corpus fetched over plain HTTP,
because both code paths bottom out in `services/register.py`.

**Growth:** the parts of this design that would need real work before a
second team could rely on it: (1) embeddings aren't actually wired into
ingestion yet (see Cuts) — real semantic retrieval is a follow-up, not a
redesign; (2) `detect_conflicts` for update runs only compares
same-run claims, not the full corpus history — broadening it is additive,
not a rewrite; (3) the worker's 1-second poll would want to become a
`LISTEN`/`NOTIFY` push once run volume justifies it, without touching the
`SKIP LOCKED` claiming logic itself.

## How the system behaves when a step fails

- **A node raises mid-graph.** The run's `runs.status` stays whatever it
  was set to before the graph started (`worker.py` sets `'running'`) — an
  honest "died mid-flight" state, indistinguishable from a real `kill -9`.
  No terminal SSE event fires, so a client watching the stream simply stops
  receiving events rather than seeing a false "completed". Every node that
  *did* finish before the crash is checkpointed; re-invoking `run_agent`
  for the same `run_id` resumes from the last completed node instead of
  re-running the graph, and never re-bills a `cost_events` row for a stage
  that already ran (`test_agent_resume.py`).
- **A single document's LLM response looks hijacked.**
  `injection_guard.scan_response` flags smells (fake system directives,
  requests to exfiltrate data, instructions to approve everything) in
  `extract`'s and `examine`'s raw responses. A hit drops that one response
  and logs a `possible_prompt_injection` finding — the rest of the
  documents in the run still get processed; the whole run doesn't fail
  over one poisoned source (`test_injection.py`).
- **A claim's cited quote doesn't actually appear in its chunk.** The
  claim is dropped, not partially trusted; a `claim_rejected_bad_quote`
  audit event records exactly what was rejected and why
  (`test_no_bluff.py`).
- **The run would exceed its cost budget.** `call_claude` raises
  `BudgetExceeded` before making the call that would cross the line, not
  after — the graph fails at that node rather than continuing to spend.
- **Two workers claim the same run.** They can't:
  `SELECT ... FOR UPDATE SKIP LOCKED` means a row already locked by one
  claiming transaction is invisible to another's, not blocked-then-double-
  claimed (`test_concurrent.py`).
- **Two runs against the same corpus try to commit register changes at the
  same time.** `commit_node` takes a Postgres advisory lock scoped to
  `corpus_id` before writing; the second transaction's read of the current
  row only happens after the first has committed, so neither write is lost
  (`test_concurrent.py`'s `test_concurrent_commits_same_corpus_do_not
  _corrupt_register_entries`).
- **A human rejects part of a review batch.** Rejecting one conflict,
  finding, or proposed register change never affects its siblings — each
  decision is applied independently, and `commit_node` only ever acts on
  items already marked `approved`/`rejected` (`test_human_gate.py`,
  `test_incremental.py`).

## Cuts, and why

Full list with reasoning: [`docs/cuts.md`](./docs/cuts.md). The ones worth
restating here because they shape what a reviewer should and shouldn't
expect from this build:

- **No auth; reviewer identity is a free-text header/field.** The brief
  doesn't ask for it, and building it would spend days on a behavior the
  brief doesn't grade.
- **No embedding step wired into ingestion.** This is the one cut I'd
  reverse first given another day — everything downstream of it (hybrid
  retrieval's vector side) is built and tested in isolation, just never
  invoked. It's a wiring gap, not a design gap.
- **Update-run conflict detection is same-run-only.** A new claim that
  contradicts a claim from a *previous* run doesn't get flagged as a
  `conflicts` row today, even though it can still change the register
  field it bears on. Correct behavior, incomplete visibility.
- **No per-claim human review in the UI.** The gate reviews conflicts,
  findings, and proposed register changes, matching the brief's "conflicts,
  findings, and updates are approved or rejected" — not every individual
  extracted claim.
- **No automated frontend tests.** Verified manually against a live
  backend each time; see `docs/cuts.md` for the exact checklist run.

## Running a second run on a different corpus

`backend/scripts/seed_demo.py` (run automatically by `make dev`/`make
seed`) populates **two** corpora with different declared-format mixes:

- `corpus/demo/` — `.md`, `.csv`, `.json`, `.txt` (6 documents, every
  `doc_type` the classifier recognizes, one real conflict, one
  owner-missing feature).
- `corpus/demo2/` — `.docx`, `.pdf`, `.json`, `.txt` (4 documents, no `.md`
  or `.csv` at all).

Both are already ingested into their own `Corpus` row by the time `make
dev` finishes. To run the second corpus:

```
# find its id
curl localhost:8000/corpora | jq '.[] | select(.name=="demo2")'

# start an initial run against it (needs a real ANTHROPIC_API_KEY in .env)
curl -X POST localhost:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{"corpus_id": "<demo2 corpus id>", "kind": "initial"}'

# make sure a worker is running to actually drive it
uv run python -m backend.app.worker
```

Or the same three calls via MCP tools (`list_corpora`, `start_run`,
`get_run`) against `python -m backend.app.mcp_server` — see
`backend/tests/test_mcp.py` for a worked example of the exact call
sequence. Either path produces a register you can spot-check field by
field against `corpus/demo2/`'s source files, same as the primary demo
corpus.
