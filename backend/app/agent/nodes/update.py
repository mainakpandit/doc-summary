"""`update` node (implementation plan 8.5, update-run path,
task_breakdown Step 30 (2)).

Two responsibilities, both scoped to `kind='update'` runs:

`discover_update_scope` runs *before* `classify`/`extract` (called from
`agent/graph.py`'s `run_agent` while it's building the fresh `AgentState`,
not from inside this graph node) to decide which documents an update run
even looks at: the triggering document, plus any other document that turns
out to be a close match for its content. "Close match" is answered with a
direct `pg_trgm` `similarity()` query against the trigger document's own
chunk text (a sample of it) rather than the fused `services/retrieval.py`
`retrieve()` -- at this point in the run nothing has been extracted yet, so
chunk text is the only signal available, and it's the same text extraction
would read anyway. `retrieve()`'s reciprocal-rank-fusion score was tried
first and rejected: RRF only ranks candidates *relative to each other*,
so on a small corpus (the common case for this MVP's demo/test corpora) the
single other document always ranks somewhere in the top-k regardless of
whether it has anything to do with the trigger document, which would
silently defeat "only re-extract on the triggering document" for exactly
the corpora this behavior is easiest to observe on. A raw similarity score
with a real floor (`NEIGHBOR_MIN_TRIGRAM_SIMILARITY`) doesn't have that
failure mode: a document scores near zero and is excluded regardless of
corpus size when it shares essentially no text with the trigger document
(see `test_incremental.py`, which introduces exactly such an unrelated
document and asserts it doesn't touch an existing feature's row). This
intentionally forgoes the embedding/vector side of retrieval for this one
purpose -- see docs/assumptions.md for the full reasoning, including why
that also sidesteps needing every update-path test to wire `fake_embedder`.

This keeps "only re-extract on the triggering document (plus
retrieval-based neighbor documents if a claim references them)"
(task_breakdown Step 30) honest without requiring a second extraction
pass: a neighbor is pulled into `state.documents` up front, so
`classify`/`extract` process it in the same pass as the trigger document,
and any claim it contributes competes on equal footing in the diff below.

`update_node` is the graph node itself, replacing `build_register` for
`kind='update'` runs (routed in `graph.py`). Unlike `build_register`
(which treats every persisted claim as fresh), this recomputes each
*affected* feature's fields -- `feature_key`s that this run's own
`state.claims` touched, via the same `build_fields` winner-selection
build_register.py uses -- from *all* persisted claims for that feature
(old and new together), then compares the result against what
`register_entries`/`register_field_sources` currently hold:

  - no existing row for the feature_key -> an addition, identical in shape
    to `build_register`'s.
  - an existing row, and the winning claim_id for a field is unchanged ->
    that field is untouched; if every field is untouched, the feature_key
    goes in `register_diff.unaffected`.
  - an existing row, and a field's winning claim_id changed -> one
    `RegisterFieldChange`, citing the new winning claim.

Feature_keys this run's claims never mention aren't examined at all --
this run has no new information about them, so it has nothing to "prove"
either way (see `RegisterDiff.unaffected`'s own docstring in
agent/state.py). That absence is exactly what makes
`commit_node` a no-op for them: they're in neither `additions` nor
`changes`, so nothing about their `register_entries` row is ever touched,
including `updated_at` (task_breakdown Step 30 (2)'s `test_incremental.py`
requirement).

Conflict detection (`detect_conflicts_node`) is unchanged by this step and
still only compares claims within the *same run* (`Claim.run_id ==
run_id`), so an update run's new claim disagreeing with a claim from a
prior run won't surface as a `conflicts` row -- broadening
`detect_conflicts` to compare against the full corpus history is future
work, logged in docs/assumptions.md rather than folded in here.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.nodes.build_register import (
    _addition_id,
    _load_claim_rows,
    build_fields,
    slugify,
)
from backend.app.agent.state import (
    AgentState,
    DocumentRef,
    RegisterDiff,
    RegisterEntryDraft,
    RegisterFieldChange,
)
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.chunk import Chunk
from backend.app.models.document import Document
from backend.app.models.register_entry import RegisterEntry

logger = structlog.get_logger(__name__)

# How many of the trigger document's own chunks seed neighbor discovery, how
# many candidates per seed chunk to consider, the minimum pg_trgm
# `similarity()` a candidate must clear to count as a real match (not just
# "the only other document in a small corpus" -- see module docstring), and
# how many distinct neighbor documents an update run's scope may grow to
# include -- all fixed, small caps chosen to bound the extra classify/extract
# cost an update run can incur beyond its one triggering document (CLAUDE.md
# behavior 10).
NEIGHBOR_SEED_CHUNK_LIMIT = 3
NEIGHBOR_CANDIDATES_PER_SEED = 5
NEIGHBOR_MIN_TRIGRAM_SIMILARITY = 0.15
NEIGHBOR_DOCUMENT_LIMIT = 3

SINGLE_VALUE_FIELDS = ("name", "owner", "target_release", "status")
MULTI_VALUE_FIELDS = ("open_risks",)


async def discover_update_scope(
    session: AsyncSession, corpus_id: uuid.UUID, trigger_doc_id: uuid.UUID
) -> list[DocumentRef]:
    """The triggering document, plus up to `NEIGHBOR_DOCUMENT_LIMIT` other
    documents whose content is a retrieval match for it. Returns just the
    trigger document (or `[]` if it doesn't exist) when nothing else in the
    corpus matches."""
    trigger_document = await session.get(Document, trigger_doc_id)
    if trigger_document is None:
        return []

    scope = [DocumentRef(id=trigger_document.id, filename=trigger_document.filename)]

    # `Document.chunks` is `lazy="selectin"` (models/document.py), so it was
    # already fetched as part of the `session.get` above -- no extra query.
    seed_chunks = trigger_document.chunks
    neighbor_ids: dict[uuid.UUID, None] = {}
    for chunk in seed_chunks[:NEIGHBOR_SEED_CHUNK_LIMIT]:
        similarity = func.similarity(Chunk.text, chunk.text)
        stmt = (
            select(Chunk.document_id)
            .join(Document, Chunk.document_id == Document.id)
            .where(
                Document.corpus_id == corpus_id,
                Document.id != trigger_doc_id,
                similarity >= NEIGHBOR_MIN_TRIGRAM_SIMILARITY,
            )
            .order_by(similarity.desc())
            .limit(NEIGHBOR_CANDIDATES_PER_SEED)
        )
        for document_id in await session.scalars(stmt):
            neighbor_ids.setdefault(document_id, None)
        if len(neighbor_ids) >= NEIGHBOR_DOCUMENT_LIMIT:
            break

    if neighbor_ids:
        capped_ids = list(neighbor_ids)[:NEIGHBOR_DOCUMENT_LIMIT]
        neighbors = (
            await session.scalars(select(Document).where(Document.id.in_(capped_ids)))
        ).all()
        scope.extend(DocumentRef(id=doc.id, filename=doc.filename) for doc in neighbors)

    return scope


def _claim_ids_for_field(fields: dict[str, Any], field_name: str) -> set[str]:
    return {s["claim_id"] for s in fields.get("sources", []) if s["field"] == field_name}


def _existing_field_claim_ids(entry: RegisterEntry) -> dict[str, set[uuid.UUID]]:
    """`entry.field_sources` is `lazy="selectin"` (models/register_entry.py),
    already loaded alongside `entry` itself -- no extra query here."""
    by_field: dict[str, set[uuid.UUID]] = {}
    for source in entry.field_sources:
        by_field.setdefault(source.field_name, set()).add(source.claim_id)
    return by_field


def _diff_existing_entry(
    run_id: uuid.UUID, entry: RegisterEntry, new_fields: dict[str, Any]
) -> list[RegisterFieldChange]:
    old_claim_ids = _existing_field_claim_ids(entry)
    changes: list[RegisterFieldChange] = []

    for field_name in SINGLE_VALUE_FIELDS:
        new_ids = {uuid.UUID(c) for c in _claim_ids_for_field(new_fields, field_name)}
        # `build_fields` picks exactly one winning claim per single-value
        # field, so a non-empty `new_ids` here is always a singleton.
        if not new_ids or new_ids == old_claim_ids.get(field_name, set()):
            continue
        changes.append(
            RegisterFieldChange(
                id=_addition_id(run_id, f"{entry.feature_key}:{field_name}"),
                feature_key=entry.feature_key,
                field_name=field_name,
                old_value=entry.fields.get(field_name),
                new_value=new_fields.get(field_name),
                claim_id=next(iter(new_ids)),
            )
        )

    for field_name in MULTI_VALUE_FIELDS:
        new_ids = {uuid.UUID(c) for c in _claim_ids_for_field(new_fields, field_name)}
        old_ids = old_claim_ids.get(field_name, set())
        # A multi-value field (open_risks) can gain more than one new
        # backing claim at once; `RegisterFieldChange.claim_id` only cites
        # one, so the lowest-UUID newcomer stands in as the representative
        # cause while `new_value` still carries the full updated list (see
        # module docstring / docs/assumptions.md).
        newly_added = sorted(new_ids - old_ids, key=str)
        if not newly_added:
            continue
        changes.append(
            RegisterFieldChange(
                id=_addition_id(run_id, f"{entry.feature_key}:{field_name}"),
                feature_key=entry.feature_key,
                field_name=field_name,
                old_value=entry.fields.get(field_name),
                new_value=new_fields.get(field_name),
                claim_id=newly_added[0],
            )
        )

    return changes


async def update_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="update")

    if state.kind != "update":
        async with AsyncSessionLocal() as session:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="update_skipped",
                    payload={"reason": "update_node only runs for kind='update'"},
                )
            )
            await session.commit()
        logger.info(
            "agent_node_exit", run_id=str(state.run_id), node="update", additions=0, changes=0
        )
        return {}

    affected_keys = sorted({slugify(c.subject) for c in state.claims})

    additions: list[RegisterEntryDraft] = []
    changes: list[RegisterFieldChange] = []
    unaffected: list[str] = []

    async with AsyncSessionLocal() as session:
        if affected_keys:
            claim_rows = await _load_claim_rows(session, state.corpus_id)
            groups: dict[str, list] = {}
            for row in claim_rows:
                key = slugify(row.subject)
                if key in affected_keys:
                    groups.setdefault(key, []).append(row)

            for feature_key in affected_keys:
                new_fields = build_fields(groups.get(feature_key, []))

                entry = (
                    await session.scalars(
                        select(RegisterEntry).where(
                            RegisterEntry.corpus_id == state.corpus_id,
                            RegisterEntry.feature_key == feature_key,
                        )
                    )
                ).first()

                if entry is None:
                    additions.append(
                        RegisterEntryDraft(
                            id=_addition_id(state.run_id, feature_key),
                            feature_key=feature_key,
                            fields=new_fields,
                        )
                    )
                    session.add(
                        AuditEvent(
                            run_id=state.run_id,
                            event_type="register_entry_proposed",
                            payload={
                                "feature_key": feature_key,
                                "fields": {k: v for k, v in new_fields.items() if k != "sources"},
                                "claim_ids": sorted({s["claim_id"] for s in new_fields["sources"]}),
                            },
                        )
                    )
                    continue

                field_changes = _diff_existing_entry(state.run_id, entry, new_fields)
                if not field_changes:
                    unaffected.append(feature_key)
                    session.add(
                        AuditEvent(
                            run_id=state.run_id,
                            event_type="register_entry_unaffected",
                            payload={
                                "feature_key": feature_key,
                                "register_entry_id": str(entry.id),
                            },
                        )
                    )
                    continue

                changes.extend(field_changes)
                for change in field_changes:
                    session.add(
                        AuditEvent(
                            run_id=state.run_id,
                            event_type="register_field_change_proposed",
                            payload={
                                "register_entry_id": str(entry.id),
                                "feature_key": change.feature_key,
                                "field": change.field_name,
                                "old": change.old_value,
                                "new": change.new_value,
                                "claim_id": str(change.claim_id),
                            },
                        )
                    )

        if not additions and not changes:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="register_diff_empty",
                    payload={"reason": "no affected register fields found for this update run"},
                )
            )

        await session.commit()

    logger.info(
        "agent_node_exit",
        run_id=str(state.run_id),
        node="update",
        additions=len(additions),
        changes=len(changes),
        unaffected=len(unaffected),
    )
    return {
        "register_diff": RegisterDiff(additions=additions, changes=changes, unaffected=unaffected)
    }
