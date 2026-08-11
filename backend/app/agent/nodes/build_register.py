"""`build_register` node (implementation plan 8.5, initial-run path).

For an `initial` run, loads every persisted claim for `state.corpus_id`
(not just this run's `state.claims` -- the spec asks for "all persisted
claims for the corpus", and an initial run's claims are exactly that
corpus's claims so far) together with the citations (`ClaimSource` ->
`Chunk` -> `Document`) that back them, groups them by a stable
`feature_key` (`slugify(lower(subject))`), and for each group picks one
winning claim per register field by highest `confidence`, ties broken by
the most recent backing `documents.ingested_at`.

Deliberately does NOT write to `register_entries` / `register_field_sources`.
`register_entries` has no status/pending column the way `findings.status`
or `conflicts.resolution` do -- every row in it is a live entry, so writing
one before a human approves it would make the "proposed" state
indistinguishable from the confirmed one, contradicting "Humans gate every
commit" (CLAUDE.md) and behavior 9's mention of a dedicated `commit` node
(advisory-locked per `corpus_id`) as the place mutations actually land.
Instead the proposal is computed here and handed to the graph purely via
`state.register_diff.additions`, which the not-yet-built `human_gate` /
`commit` nodes read to do the real insert once a human confirms. Logged as
an assumption in docs/assumptions.md since the task description's "Insert
register_entries and register_field_sources rows" reads, taken alone, like
this node should write them.

Each field's value cites the winning claim's sources under
`fields["sources"]` (one entry per `field`, `claim_id`, backing chunk, and
verbatim quote) -- CLAUDE.md behavior 5, and the raw material the future
`commit` node needs to populate `register_field_sources` per field/claim.
`open_risks` is additive rather than single-winner: every claim with
predicate `"risk"` contributes its object (deduplicated, most-confident
first) since a feature can have more than one open risk at once -- no
predicate vocabulary for this exists yet in `corpus/demo/rules.yaml` or
the extract prompts, so `"risk"` is this node's own assumption, also
logged in docs/assumptions.md.

`update`-kind runs are out of this step's scope (the plan lists
`build_register` and `update` as two separate 8.5 pieces); this node
no-ops for them, writing a `build_register_skipped` audit event so the
gap is visible rather than silent.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select

from backend.app.agent.state import AgentState, RegisterDiff, RegisterEntryDraft
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.chunk import Chunk
from backend.app.models.claim import Claim, ClaimSource
from backend.app.models.document import Document

logger = structlog.get_logger(__name__)

RISK_PREDICATE = "risk"
SINGLE_VALUE_FIELDS = ("owner", "target_release", "status")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_EPOCH = datetime.min.replace(tzinfo=UTC)

# Fixed namespace for deriving RegisterEntryDraft.id deterministically from
# (run_id, feature_key) -- see that field's docstring in agent/state.py for
# why a proposal needs a stable id before any register_entries row exists.
_REGISTER_ITEM_NAMESPACE = uuid.UUID("8f2f6b1a-2c3d-4e5f-9a1b-6c7d8e9f0a1b")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "unknown"


def _addition_id(run_id: uuid.UUID, feature_key: str) -> uuid.UUID:
    return uuid.uuid5(_REGISTER_ITEM_NAMESPACE, f"{run_id}:{feature_key}")


@dataclass
class SourceCitation:
    chunk_id: uuid.UUID
    quote: str
    document: str


@dataclass
class ClaimRow:
    id: uuid.UUID
    subject: str
    predicate: str
    object: str
    confidence: float
    ingested_at: datetime
    citations: list[SourceCitation] = field(default_factory=list)


async def _load_claim_rows(session, corpus_id: uuid.UUID) -> list[ClaimRow]:
    rows = (
        await session.execute(
            select(
                Claim.id,
                Claim.subject,
                Claim.predicate,
                Claim.object,
                Claim.confidence,
                ClaimSource.chunk_id,
                ClaimSource.quote,
                Document.filename,
                Document.ingested_at,
            )
            .select_from(Claim)
            .join(ClaimSource, ClaimSource.claim_id == Claim.id)
            .join(Chunk, Chunk.id == ClaimSource.chunk_id)
            .join(Document, Document.id == Chunk.document_id)
            .where(Claim.corpus_id == corpus_id)
            .order_by(Claim.id)
        )
    ).all()

    by_claim: dict[uuid.UUID, ClaimRow] = {}
    for (
        claim_id,
        subject,
        predicate,
        obj,
        confidence,
        chunk_id,
        quote,
        filename,
        ingested_at,
    ) in rows:
        citation = SourceCitation(chunk_id=chunk_id, quote=quote, document=filename)
        existing = by_claim.get(claim_id)
        if existing is None:
            by_claim[claim_id] = ClaimRow(
                id=claim_id,
                subject=subject,
                predicate=predicate,
                object=obj,
                confidence=confidence,
                ingested_at=ingested_at or _EPOCH,
                citations=[citation],
            )
        else:
            existing.citations.append(citation)
            if ingested_at and ingested_at > existing.ingested_at:
                existing.ingested_at = ingested_at

    return list(by_claim.values())


def _group_by_feature(claim_rows: list[ClaimRow]) -> dict[str, list[ClaimRow]]:
    groups: dict[str, list[ClaimRow]] = {}
    for row in claim_rows:
        groups.setdefault(slugify(row.subject), []).append(row)
    return groups


def _select_best(claims: list[ClaimRow], predicate: str | None = None) -> ClaimRow | None:
    candidates = claims if predicate is None else [c for c in claims if c.predicate == predicate]
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c.confidence, c.ingested_at))


def _collect_risks(claims: list[ClaimRow]) -> list[ClaimRow]:
    risk_claims = sorted(
        (c for c in claims if c.predicate == RISK_PREDICATE),
        key=lambda c: (c.confidence, c.ingested_at),
        reverse=True,
    )
    seen: set[str] = set()
    ordered: list[ClaimRow] = []
    for claim in risk_claims:
        if claim.object not in seen:
            seen.add(claim.object)
            ordered.append(claim)
    return ordered


def build_fields(claims: list[ClaimRow]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []

    def _cite(field_name: str, claim: ClaimRow) -> None:
        for citation in claim.citations:
            sources.append(
                {
                    "field": field_name,
                    "claim_id": str(claim.id),
                    "document": citation.document,
                    "chunk_id": str(citation.chunk_id),
                    "quote": citation.quote,
                }
            )

    name_claim = _select_best(claims)
    fields: dict[str, Any] = {
        "name": name_claim.subject if name_claim else None,
        "owner": None,
        "target_release": None,
        "status": None,
        "open_risks": [],
    }
    if name_claim is not None:
        _cite("name", name_claim)

    for field_name in SINGLE_VALUE_FIELDS:
        best = _select_best(claims, field_name)
        if best is not None:
            fields[field_name] = best.object
            _cite(field_name, best)

    risks = _collect_risks(claims)
    fields["open_risks"] = [c.object for c in risks]
    for risk_claim in risks:
        _cite("open_risks", risk_claim)

    fields["sources"] = sources
    return fields


async def build_register_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="build_register")

    if state.kind != "initial":
        async with AsyncSessionLocal() as session:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="build_register_skipped",
                    payload={
                        "reason": (
                            "update-run register logic is not implemented by build_register "
                            "yet, see docs/assumptions.md"
                        )
                    },
                )
            )
            await session.commit()

        logger.info("agent_node_exit", run_id=str(state.run_id), node="build_register", additions=0)
        return {}

    async with AsyncSessionLocal() as session:
        claim_rows = await _load_claim_rows(session, state.corpus_id)
        groups = _group_by_feature(claim_rows)

        additions: list[RegisterEntryDraft] = []
        for feature_key in sorted(groups):
            fields = build_fields(groups[feature_key])
            additions.append(
                RegisterEntryDraft(
                    id=_addition_id(state.run_id, feature_key),
                    feature_key=feature_key,
                    fields=fields,
                )
            )

            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="register_entry_proposed",
                    payload={
                        "feature_key": feature_key,
                        "fields": {k: v for k, v in fields.items() if k != "sources"},
                        "claim_ids": sorted({s["claim_id"] for s in fields["sources"]}),
                    },
                )
            )

        if not additions:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="register_diff_empty",
                    payload={"reason": "no persisted claims found for this corpus"},
                )
            )

        await session.commit()

    logger.info(
        "agent_node_exit",
        run_id=str(state.run_id),
        node="build_register",
        additions=len(additions),
    )
    return {"register_diff": RegisterDiff(additions=additions)}
