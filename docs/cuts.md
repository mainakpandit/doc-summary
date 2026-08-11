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
