# PM Document Analyst

An agentic system that ingests a pile of PM documents, extracts sourced
claims, detects conflicts, checks a rules playbook, and produces a Feature
Register. Every commit to the register is gated by a human. New documents
trigger incremental updates, not rewrites.

Full design: [`IMPLEMENTATION_PLAN.md`](./IMPLEMENTATION_PLAN.md). Build
order: [`TASK_BREAKDOWN.md`](./TASK_BREAKDOWN.md).

## Scope

**Domain:** software engineering documents for product managers (SWE-for-PM).
The pile a PM lives inside — PRDs, tech specs/RFCs, sprint plans, ticket
exports, meeting notes, release notes, and incident postmortems — is
ingested and reconciled into a single **Feature Register**: one row per
feature/epic, with scope, owner, target release, status, open risks, and a
source-of-truth link for every field.

**Accepted formats**, declared explicitly per the brief:

| Extension | Typical source |
|---|---|
| `.md`   | PRDs, RFCs, release notes |
| `.txt`  | Meeting notes, transcripts |
| `.pdf`  | Tech specs, exported docs |
| `.docx` | PRDs authored in Word |
| `.csv`  | JIRA/Linear ticket exports |
| `.json` | Structured ticket/API exports |

Any file outside this set is rejected with a clear error message at
ingestion time — it is never silently skipped.

## Setup

The project is scaffolding only at this point; no dependencies are pinned
yet and there is no working code to run.

Once the backend and frontend land, the one documented command to go from a
fresh clone to a running system will be:

```
make dev
```

This will stand up Postgres via Docker Compose, install Python and Node
dependencies, run migrations, seed the demo corpus, and start the backend
and frontend dev servers. See section 5.3 of `IMPLEMENTATION_PLAN.md` for
the exact steps that command will perform.

## Assumptions

_None recorded yet._ Ambiguous calls made during the build will be logged
here (and in detail in `docs/assumptions.md`) as they happen.

## Cuts

_None recorded yet._ Anything cut for scope, and why, will be logged here
(and in detail in `docs/cuts.md`) as they happen.
