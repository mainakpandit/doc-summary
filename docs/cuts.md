# Cuts

Deliberate scope cuts made during the build, logged as they happen.

- **No automated frontend tests for MVP.** `frontend/src/pages/RunsList.tsx`
  and `frontend/src/pages/RunDetail.tsx` (plus the `EventSource` wrapper in
  `frontend/src/api/sse.ts`) were verified manually instead: `make dev`
  against a live Postgres, a real corpus + run created through the API, the
  worker driving it through every graph stage, and a headless-Chromium pass
  over the running Vite dev server confirming the runs table (id, corpus
  name resolved from `/corpora`, kind, status badge, started_at, polled
  cost), row-click routing to `/runs/:id`, the SSE-driven stage cards
  (pending → running → done, expandable audit payload), and the
  `awaiting_review` → "Review now" banner (the last verified by flipping a
  test run's `status` directly in the dev DB, since producing a real
  `awaiting_review` run needs an `ANTHROPIC_API_KEY` this environment
  doesn't have). No Vitest/RTL/Playwright suite was added. Consistent with
  CLAUDE.md's `make test` requirement, which only covers the backend.

- **Reviewer identity is a plain text input in the top nav (`NavBar.tsx`),
  not auth.** Per CLAUDE.md's "do not add auth" and the `X-Reviewer` header
  convention it names, whatever name is typed there is: persisted to
  `localStorage` via a Zustand store (`frontend/src/store/reviewer.ts`) so
  it survives reloads, sent as a literal `X-Reviewer` header on the review
  submission request, and also written into that request's `reviewer` body
  field (see docs/assumptions.md -- the actual endpoint schema requires it
  there). Nothing checks the value is a real person, unique, or the same
  person across requests; `ReviewGate.tsx` only disables "Submit decisions"
  when the field is empty. `Review.reviewer` / `Finding.reviewer` /
  `Conflict.resolved_by` end up holding exactly whatever string was typed,
  which is the intended MVP behavior, not a bug.

- **No embedding step is wired into ingestion, the watcher, or the graph.**
  `services/embeddings.py`'s `embed_chunks` exists, is cost-tracked, and is
  tested in isolation (`test_embeddings.py`), but nothing calls it outside
  tests -- not `services/ingestion.ingest_file`, not `services/watcher.py`,
  not any graph node. `services/retrieval.retrieve`'s vector side is
  therefore always empty in this build; hybrid retrieval degrades to
  pg_trgm keyword search alone wherever it's used (`agent/nodes/update.py`'s
  neighbor discovery deliberately avoids even that, using a direct trigram
  query for its own reasons -- see docs/assumptions.md). The second-order
  effect: real semantic ("said differently, means the same thing") neighbor
  matching for update runs doesn't work in this build, only literal
  keyword/phrase overlap does. Wiring `embed_chunks` into the watcher (or
  ingestion generally) is the natural next step; cut here for time, not
  because it's hard.

- **Update-run conflict detection only sees claims from within the
  triggering run itself, not the full corpus history.** See
  docs/assumptions.md's `detect_conflicts_node` entry. Practically: if an
  update run's new document contradicts a claim from a *previous* run, no
  `conflicts` row is created for it today -- the register diff still
  reflects the new information (the field's winning claim can still
  change), but the disagreement itself isn't surfaced as a reviewable
  conflict the way two claims landing in the *same* run would be.

- **A `RegisterFieldChange` for `open_risks` cites one representative new
  claim, not every new risk claim at once, when an update run adds more
  than one open risk to the same feature in the same pass.** See
  docs/assumptions.md. The full updated risk list still lands in the
  register correctly; only the citation on that one review item is
  incomplete in the rare multi-new-risk-at-once case.

- **No `docs/demo.mp4`.** This build was produced in a sandboxed, headless
  environment with no display and no screen-recording tool — there was no
  way to record one honestly. `docs/demo.md` is the documented substitute
  the task text explicitly allows ("or `docs/demo.md` with a link"): the
  exact walkthrough a recording would show, with every step also covered
  by an automated test (`test_mcp.py`, `test_human_gate.py`,
  `test_incremental.py`) so the flow is proven even though it isn't
  filmed.
