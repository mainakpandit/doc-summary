"""`commit` node (implementation plan 8.7, task_breakdown Step 25).

Writes every `approved` `register_diff` item (additions and field changes)
to `register_entries` / `register_field_sources`, and an `audit_events` row
per mutation (CLAUDE.md: "every register field mutation writes an
audit_events row with the backing claim_ids and chunk_ids") plus one per
`rejected` item so the exclusion itself is traceable. `human_gate_node`
never lets this node run while any item is still `pending` (see its
docstring) -- everything reaching here is either `approved` or `rejected`,
so this node only ever applies decisions already made, never waits on one.

Acquires a Postgres advisory lock on `corpus_id` for its transaction
(CLAUDE.md behavior 9: "Same-corpus concurrent updates use an advisory lock
on corpus_id in the commit node") whenever there's real register work to do,
so two runs against the same corpus finishing review at the same time can't
race on the same `register_entries` row. `pg_advisory_xact_lock` releases
automatically at transaction end, so no explicit unlock is needed.

`RegisterFieldChange` handling (the update-run path) is implemented for
completeness -- `register_diff.changes` is always empty today since the
`update` node (task_breakdown Step 29) doesn't exist yet, so this path is
unexercised until then; see docs/assumptions.md.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.state import AgentState, RegisterEntryDraft, RegisterFieldChange
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.register_entry import RegisterEntry, RegisterFieldSource

logger = structlog.get_logger(__name__)


async def _advisory_lock(session: AsyncSession, corpus_id: uuid.UUID) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(str(corpus_id)))))


async def _commit_addition(
    session: AsyncSession, state: AgentState, addition: RegisterEntryDraft
) -> None:
    fields = {k: v for k, v in addition.fields.items() if k != "sources"}
    sources = addition.fields.get("sources", [])

    entry = RegisterEntry(
        corpus_id=state.corpus_id,
        feature_key=addition.feature_key,
        fields=fields,
        last_updated_run_id=state.run_id,
    )
    session.add(entry)
    await session.flush()  # assign entry.id for register_field_sources' FK

    claim_ids: set[str] = set()
    for source in sources:
        session.add(
            RegisterFieldSource(
                register_entry_id=entry.id,
                field_name=source["field"],
                claim_id=uuid.UUID(source["claim_id"]),
            )
        )
        claim_ids.add(source["claim_id"])

    session.add(
        AuditEvent(
            run_id=state.run_id,
            event_type="register_entry_committed",
            payload={
                "register_entry_id": str(entry.id),
                "feature_key": addition.feature_key,
                "fields": fields,
                "claim_ids": sorted(claim_ids),
            },
        )
    )


async def _reject_addition(
    session: AsyncSession, state: AgentState, addition: RegisterEntryDraft
) -> None:
    session.add(
        AuditEvent(
            run_id=state.run_id,
            event_type="register_entry_rejected",
            payload={"item_id": str(addition.id), "feature_key": addition.feature_key},
        )
    )


async def _commit_field_change(
    session: AsyncSession, state: AgentState, change: RegisterFieldChange
) -> None:
    entry = (
        await session.scalars(
            select(RegisterEntry).where(
                RegisterEntry.corpus_id == state.corpus_id,
                RegisterEntry.feature_key == change.feature_key,
            )
        )
    ).first()
    if entry is None:
        # A field change always targets an existing entry; no row for this
        # feature_key means there's nothing to apply this to. Log and move
        # on rather than fail the whole run over one sibling's bad state.
        session.add(
            AuditEvent(
                run_id=state.run_id,
                event_type="register_field_change_skipped",
                payload={
                    "feature_key": change.feature_key,
                    "reason": "no register_entries row found",
                },
            )
        )
        return

    entry.fields = {**entry.fields, change.field_name: change.new_value}
    entry.version += 1
    entry.last_updated_run_id = state.run_id
    entry.updated_at = datetime.now(UTC)

    await session.execute(
        pg_insert(RegisterFieldSource)
        .values(register_entry_id=entry.id, field_name=change.field_name, claim_id=change.claim_id)
        .on_conflict_do_nothing()
    )

    session.add(
        AuditEvent(
            run_id=state.run_id,
            event_type="register_field_updated",
            payload={
                "register_entry_id": str(entry.id),
                "feature_key": change.feature_key,
                "field": change.field_name,
                "old": change.old_value,
                "new": change.new_value,
                "backed_by_claim_ids": [str(change.claim_id)],
            },
        )
    )


async def _reject_field_change(
    session: AsyncSession, state: AgentState, change: RegisterFieldChange
) -> None:
    session.add(
        AuditEvent(
            run_id=state.run_id,
            event_type="register_field_change_rejected",
            payload={
                "item_id": str(change.id),
                "feature_key": change.feature_key,
                "field_name": change.field_name,
            },
        )
    )


async def commit_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="commit")

    diff = state.register_diff
    committed = 0
    rejected = 0

    async with AsyncSessionLocal() as session:
        if diff is not None and (diff.additions or diff.changes):
            await _advisory_lock(session, state.corpus_id)

            for addition in diff.additions:
                if addition.status == "approved":
                    await _commit_addition(session, state, addition)
                    committed += 1
                elif addition.status == "rejected":
                    await _reject_addition(session, state, addition)
                    rejected += 1

            for change in diff.changes:
                if change.status == "approved":
                    await _commit_field_change(session, state, change)
                    committed += 1
                elif change.status == "rejected":
                    await _reject_field_change(session, state, change)
                    rejected += 1

        if committed == 0 and rejected == 0:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="commit_skipped",
                    payload={"reason": "nothing to commit"},
                )
            )

        await session.commit()

    logger.info(
        "agent_node_exit",
        run_id=str(state.run_id),
        node="commit",
        committed=committed,
        rejected=rejected,
    )
    return {}
