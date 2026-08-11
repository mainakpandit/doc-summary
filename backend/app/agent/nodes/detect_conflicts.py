"""`detect_conflicts` node (implementation plan 8.3).

Groups this run's `claims` by `(subject, predicate)`; any group asserting
more than one distinct `object` is a conflict. For every pair of claims in
such a group whose objects differ, writes one `conflicts` row
(`resolution='unresolved'`) and one `conflict_detected` audit event.

The grouping, pairing, and insert all happen in a single SQL statement
(one `INSERT ... FROM SELECT` built from stacked CTEs) rather than as a
Python loop over query results. Postgres doesn't allow `COUNT(DISTINCT
x)` inside a window function, so the distinct-object count per group is
computed the window-function way instead: `DENSE_RANK() OVER (PARTITION
BY subject, predicate ORDER BY object)` gives claims sharing an object
the same rank within their group, and `MAX(that rank) OVER (PARTITION BY
subject, predicate)` is then the number of distinct objects in the group.
Groups with `max rank > 1` self-join on `(subject, predicate)` with
`a.id < b.id AND a.object <> b.object` to emit exactly one row per
conflicting pair (the `id` ordering avoids emitting both `(a, b)` and
`(b, a)`; the `object <>` filter skips pairs that happen to agree).
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import Insert, and_, func, insert, literal, select

from backend.app.agent.state import AgentState, ConflictDraft
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.claim import Claim
from backend.app.models.conflict import Conflict

logger = structlog.get_logger(__name__)


def _build_insert_stmt(run_id: uuid.UUID) -> Insert:
    run_claims = (
        select(Claim.id, Claim.subject, Claim.predicate, Claim.object)
        .where(Claim.run_id == run_id)
        .cte("run_claims")
    )

    ranked = select(
        run_claims.c.id,
        run_claims.c.subject,
        run_claims.c.predicate,
        run_claims.c.object,
        func.dense_rank()
        .over(
            partition_by=(run_claims.c.subject, run_claims.c.predicate),
            order_by=run_claims.c.object,
        )
        .label("object_rank"),
    ).cte("ranked")

    scored = select(
        ranked.c.id,
        ranked.c.subject,
        ranked.c.predicate,
        ranked.c.object,
        func.max(ranked.c.object_rank)
        .over(partition_by=(ranked.c.subject, ranked.c.predicate))
        .label("distinct_object_count"),
    ).cte("scored")

    qualifying = (
        select(scored.c.id, scored.c.subject, scored.c.predicate, scored.c.object)
        .where(scored.c.distinct_object_count > 1)
        .cte("qualifying")
    )

    a = qualifying.alias("a")
    b = qualifying.alias("b")

    pairs = select(
        literal(run_id, type_=Claim.run_id.type).label("run_id"),
        a.c.subject,
        a.c.predicate,
        a.c.id.label("claim_a_id"),
        b.c.id.label("claim_b_id"),
        literal("unresolved", type_=Conflict.resolution.type).label("resolution"),
    ).select_from(
        a.join(
            b,
            and_(
                a.c.subject == b.c.subject,
                a.c.predicate == b.c.predicate,
                a.c.id < b.c.id,
                a.c.object != b.c.object,
            ),
        )
    )

    return (
        insert(Conflict)
        .from_select(
            ["run_id", "subject", "predicate", "claim_a_id", "claim_b_id", "resolution"],
            pairs,
        )
        .returning(
            Conflict.id,
            Conflict.subject,
            Conflict.predicate,
            Conflict.claim_a_id,
            Conflict.claim_b_id,
        )
    )


async def detect_conflicts_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="detect_conflicts")

    conflicts: list[ConflictDraft] = list(state.conflicts)

    async with AsyncSessionLocal() as session:
        created = (await session.execute(_build_insert_stmt(state.run_id))).all()

        for row in created:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="conflict_detected",
                    payload={
                        "conflict_id": str(row.id),
                        "subject": row.subject,
                        "predicate": row.predicate,
                        "claim_a_id": str(row.claim_a_id),
                        "claim_b_id": str(row.claim_b_id),
                    },
                )
            )
            conflicts.append(
                ConflictDraft(
                    id=row.id,
                    subject=row.subject,
                    predicate=row.predicate,
                    claim_a_id=row.claim_a_id,
                    claim_b_id=row.claim_b_id,
                    resolution="unresolved",
                )
            )

        await session.commit()

    logger.info(
        "agent_node_exit",
        run_id=str(state.run_id),
        node="detect_conflicts",
        conflicts=len(created),
    )
    return {"conflicts": conflicts}
