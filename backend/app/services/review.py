"""Human-gate review payload + decision application (implementation plan
8.6/8.7, task_breakdown Step 25, behavior 3).

Both `agent/nodes/human_gate.py` (building the `interrupt()` payload) and
`api/reviews.py`'s `GET /runs/{id}/review` (building the HTTP response) call
`build_review_payload` so the two always agree on exactly what's pending and
how citations are shaped -- CLAUDE.md's "HTTP and MCP call the same service
layer; no logic in route handlers" extends naturally to the graph node too.

Register-change items (additions/changes from `state.register_diff`) don't
have a real DB row before `commit`, so they're identified by the
deterministic `RegisterEntryDraft.id`/`RegisterFieldChange.id` computed once
in `build_register.py` (see its docstring and `agent/state.py`).
`apply_register_decisions` is what makes a register-change decision survive
resume: since `register_diff` lives only in the LangGraph checkpoint (no
dedicated register-proposal table), the only durable record of a decision is
its `reviews` row, so every draft's `status` is re-derived from `reviews`
every time this runs -- including on `human_gate`'s resume replay -- rather
than mutated once and hoped to stick.

Conflicts and findings, by contrast, already have real rows by the time
`human_gate` runs (`detect_conflicts` and `examine` wrote them), so their
decisions are applied directly to `conflicts.resolution` /
`findings.status` by `submit_review_decisions` at `POST /runs/{id}/review`
time -- no replay step needed, `human_gate` just reads the current row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.state import RegisterDiff
from backend.app.models.chunk import Chunk
from backend.app.models.conflict import Conflict
from backend.app.models.document import Document
from backend.app.models.finding import Finding, finding_sources
from backend.app.models.review import Review
from backend.app.schemas.review import ReviewItemDecision
from backend.app.services.citations import build_citation, claim_citations as _claim_citations


class UnknownReviewItemError(Exception):
    """Raised by `submit_review_decisions` when a submitted item id/type
    doesn't match a conflict or finding row for this run -- almost always a
    stale client payload (the item was already decided elsewhere, or
    belongs to a different run). `register_change` items have no backing
    row to validate against here; `api/reviews.py` validates those against
    `build_review_payload`'s current pending set before calling this."""


async def _load_pending_conflicts(session: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    conflicts = (
        await session.scalars(
            select(Conflict).where(Conflict.run_id == run_id, Conflict.resolution == "unresolved")
        )
    ).all()

    items: list[dict[str, Any]] = []
    for conflict in conflicts:
        items.append(
            {
                "id": str(conflict.id),
                "item_type": "conflict",
                "subject": conflict.subject,
                "predicate": conflict.predicate,
                "claim_a": {
                    "id": str(conflict.claim_a_id),
                    "object": conflict.claim_a.object,
                    "confidence": conflict.claim_a.confidence,
                    "sources": await _claim_citations(session, conflict.claim_a_id),
                },
                "claim_b": {
                    "id": str(conflict.claim_b_id),
                    "object": conflict.claim_b.object,
                    "confidence": conflict.claim_b.confidence,
                    "sources": await _claim_citations(session, conflict.claim_b_id),
                },
            }
        )
    return items


async def _load_pending_findings(session: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    findings = (
        await session.scalars(
            select(Finding).where(Finding.run_id == run_id, Finding.status == "pending")
        )
    ).all()

    items: list[dict[str, Any]] = []
    for finding in findings:
        rows = (
            await session.execute(
                select(finding_sources.c.chunk_id, finding_sources.c.claim_id).where(
                    finding_sources.c.finding_id == finding.id
                )
            )
        ).all()
        sources: list[dict[str, Any]] = []
        for chunk_id, claim_id in rows:
            if claim_id is not None:
                sources.extend(await _claim_citations(session, claim_id))
            elif chunk_id is not None:
                chunk = await session.get(Chunk, chunk_id)
                if chunk is not None:
                    document = await session.get(Document, chunk.document_id)
                    sources.append(build_citation(chunk, document.filename if document else "", ""))
        items.append(
            {
                "id": str(finding.id),
                "item_type": "finding",
                "rule_id": finding.rule_id,
                "severity": finding.severity,
                "subject": finding.subject,
                "message": finding.message,
                "sources": sources,
            }
        )
    return items


async def _enrich_register_sources(
    session: AsyncSession, sources: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for source in sources:
        chunk = await session.get(Chunk, uuid.UUID(source["chunk_id"]))
        if chunk is None:
            enriched.append(source)
            continue
        document = await session.get(Document, chunk.document_id)
        citation = build_citation(
            chunk, document.filename if document else source.get("document", ""), source["quote"]
        )
        enriched.append({**source, **citation})
    return enriched


async def apply_register_decisions(
    session: AsyncSession, run_id: uuid.UUID, register_diff: RegisterDiff | None
) -> RegisterDiff:
    """Re-derive every addition's/change's `status` from its `reviews` row,
    if any -- called on every `human_gate` execution, initial pass and every
    resume replay alike, so `POST /runs/{id}/review` decisions always win
    over whatever `status` the draft already carried in the checkpoint."""
    if register_diff is None:
        return RegisterDiff()

    item_ids = [a.id for a in register_diff.additions] + [c.id for c in register_diff.changes]
    decisions: dict[uuid.UUID, str] = {}
    if item_ids:
        rows = (
            await session.execute(
                select(Review.item_id, Review.decision).where(
                    Review.run_id == run_id,
                    Review.item_type == "register_change",
                    Review.item_id.in_(item_ids),
                )
            )
        ).all()
        decisions = dict(rows)

    def _status(item_id: uuid.UUID, current: str) -> Literal["pending", "approved", "rejected"]:
        decision = decisions.get(item_id)
        if decision == "approve":
            return "approved"
        if decision == "reject":
            return "rejected"
        return current  # type: ignore[return-value]

    return RegisterDiff(
        additions=[
            a.model_copy(update={"status": _status(a.id, a.status)})
            for a in register_diff.additions
        ],
        changes=[
            c.model_copy(update={"status": _status(c.id, c.status)}) for c in register_diff.changes
        ],
        unaffected=register_diff.unaffected,
    )


async def _pending_register_items(
    session: AsyncSession, register_diff: RegisterDiff
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for addition in register_diff.additions:
        if addition.status != "pending":
            continue
        fields = {k: v for k, v in addition.fields.items() if k != "sources"}
        sources = await _enrich_register_sources(session, addition.fields.get("sources", []))
        items.append(
            {
                "id": str(addition.id),
                "item_type": "register_change",
                "change_kind": "addition",
                "feature_key": addition.feature_key,
                "fields": fields,
                "sources": sources,
            }
        )
    for change in register_diff.changes:
        if change.status != "pending":
            continue
        items.append(
            {
                "id": str(change.id),
                "item_type": "register_change",
                "change_kind": "field_change",
                "feature_key": change.feature_key,
                "field_name": change.field_name,
                "old_value": change.old_value,
                "new_value": change.new_value,
                "sources": await _claim_citations(session, change.claim_id),
            }
        )
    return items


@dataclass
class ReviewPayload:
    register_diff: RegisterDiff
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    register_changes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.conflicts or self.findings or self.register_changes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "conflicts": self.conflicts,
            "findings": self.findings,
            "register_changes": self.register_changes,
        }


async def build_review_payload(
    session: AsyncSession, run_id: uuid.UUID, register_diff: RegisterDiff | None
) -> ReviewPayload:
    resolved_diff = await apply_register_decisions(session, run_id, register_diff)
    conflicts = await _load_pending_conflicts(session, run_id)
    findings = await _load_pending_findings(session, run_id)
    register_changes = await _pending_register_items(session, resolved_diff)
    return ReviewPayload(
        register_diff=resolved_diff,
        conflicts=conflicts,
        findings=findings,
        register_changes=register_changes,
    )


async def submit_review_decisions(
    session: AsyncSession, run_id: uuid.UUID, items: list[ReviewItemDecision], reviewer: str
) -> None:
    """Write one `reviews` row per item and, for `conflict`/`finding`
    items, apply the decision straight to the underlying row.

    Conflicts only expose a binary approve/reject at the gate (no UI for
    picking claim A vs. claim B specifically -- see IMPLEMENTATION_PLAN.md
    10.3's approve/reject-only review item), so "approve" maps to
    `resolution='kept_both'` (both claims stand, the ambiguity is
    acknowledged) and "reject" maps to `'rejected_both'` (neither survives).
    This is a defensible simplification of the fuller `kept_a`/`kept_b`
    enum the schema supports, logged in docs/assumptions.md.

    `register_change` items get only their `reviews` row here -- there is
    no row to mutate yet (see module docstring); `human_gate_node` re-reads
    `reviews` on its next execution and folds the decision into
    `register_diff` for `commit` to act on.
    """
    now = datetime.now(UTC)

    for item in items:
        session.add(
            Review(
                run_id=run_id,
                item_type=item.item_type,
                item_id=item.id,
                decision=item.decision,
                note=item.note,
                reviewer=reviewer,
            )
        )

        if item.item_type == "conflict":
            conflict = await session.get(Conflict, item.id)
            if conflict is None or conflict.run_id != run_id:
                raise UnknownReviewItemError(f"conflict {item.id} not found for run {run_id}")
            conflict.resolution = "kept_both" if item.decision == "approve" else "rejected_both"
            conflict.resolved_by = f"human:{reviewer}"
            conflict.resolved_at = now
        elif item.item_type == "finding":
            finding = await session.get(Finding, item.id)
            if finding is None or finding.run_id != run_id:
                raise UnknownReviewItemError(f"finding {item.id} not found for run {run_id}")
            finding.status = "approved" if item.decision == "approve" else "rejected"
            finding.reviewer = reviewer
            finding.reviewed_at = now
        elif item.item_type != "register_change":
            raise UnknownReviewItemError(f"unknown item_type {item.item_type!r}")

    await session.commit()
