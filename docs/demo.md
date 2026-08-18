# Demo walkthrough

No screen capture is included: this build was produced in a sandboxed,
headless environment with no display and no screen-recording tool
available, so `docs/demo.mp4` would have to be faked to exist, and CLAUDE.md
is explicit that a logged cut beats a hidden gap. This is that cut — see
`docs/cuts.md`. What follows instead is the exact walkthrough a recording
would have shown, runnable end to end on a real clone with real API keys;
every step below is also what `backend/tests/test_mcp.py` and
`backend/tests/test_human_gate.py` exercise automatically (with FakeLLM
standing in for Claude), so the flow is proven to work even though it
isn't filmed.

## 1. Boot

```
git clone <repo> && cd pm-analyst
cp .env.example .env   # fill in ANTHROPIC_API_KEY and VOYAGE_API_KEY for a real run
make dev
```

Opens Postgres, runs migrations, seeds+ingests `corpus/demo/` (6 documents)
and `corpus/demo2/` (4 documents, a different format mix), and starts the
backend (`:8000`) and frontend (`:5173`). Start the worker in a second
terminal — nothing runs without it:

```
uv run python -m backend.app.worker
```

## 2. Start a run

Open http://localhost:5173, pick the `demo` corpus, start an **initial**
run. `RunDetail` opens an SSE stream and shows each stage
(`classify` → `extract` → `detect_conflicts` → `examine` → `build_register`)
turn from pending to running to done, live, with the audit payload for each
expandable — this is CLAUDE.md behavior 1 ("visible steps") made visible.

## 3. Watch it reach the human gate

Because `corpus/demo/` contains a genuine conflict ("Checkout Redesign"'s
target release disagrees between the PRD and the tech spec) and an
owner-missing feature ("Notifications Revamp"), the run stops at
`awaiting_review` instead of finishing straight through. The UI shows a
"Review now" banner.

## 4. Review

`ReviewGate` shows three sections: the conflict, the `every_feature_has
_owner` finding for "Notifications Revamp", and the proposed register
additions for all three features. Approve the conflict (both claims
stand — `kept_both`), approve two of the three additions, reject one, add
a note. Submit. The rejected addition never lands in the register; the two
approved ones do — independently of each other and of the conflict/finding
decisions (behavior 3, "human gate... rejecting one item does not affect
siblings").

## 5. Register

`Register.tsx` shows the committed features with owner/target
release/status/open risks, each cell's info icon popping the backing claim
and its verbatim source quote + a link into `SourceViewer`, scrolled and
highlighted at the exact cited span. "Download CSV" flattens it
client-side.

## 6. Drop a new file in the inbox

```
cp corpus/demo2/meeting_notes_q4_planning.txt corpus/inbox/a_new_note.txt
uv run python -m backend.app.services.watcher   # if not already running
```

Within two seconds of the file's size settling, an `update` run appears
against whichever corpus's `inbox_path` matches `corpus/inbox/` (the seeded
`demo` corpus). Watch it process just that one file (plus any
similarity-matched neighbor — see `agent/nodes/update.py`), diff against
the existing register, and propose only what actually changed. Any
feature the new file doesn't mention is untouched — its `updated_at`
doesn't move (`test_incremental.py` proves this directly).

## 7. Drive the same flow without the UI at all

```
uv run python -m backend.app.mcp_server
```

`backend/tests/test_mcp.py` is the executable version of "prove this
works": it spawns exactly this process, creates a corpus, adds a document,
starts a run, waits for the (in-process, for the test) worker to reach
`awaiting_review`, reviews and resumes — all through MCP tool calls only —
then asserts the resulting register matches the same corpus fetched over
plain HTTP.

## 8. Cost and audit

```
curl localhost:8000/runs/<run-id>/cost   # per-stage tokens/latency/USD, summing to the total
curl localhost:8000/runs/<run-id>/audit  # every event, in time order, from classify_start to run_completed
```
