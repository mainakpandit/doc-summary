"""Run lifecycle service: creation with idempotency, listing, and the
detail/cost/audit read-models `GET /runs/*` returns (task_breakdown Step
24). Kept separate from `agent/graph.py`, which drives a run once it
exists -- this module only touches the `runs` row and derived read-only
aggregates, so the future MCP server's `start_run`/`get_run`/`get_cost`/
`get_audit` tools can share it (CLAUDE.md: one service layer for HTTP and
MCP).
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.audit import AuditEvent
from backend.app.models.claim import Claim
from backend.app.models.conflict import Conflict
from backend.app.models.cost import CostEvent
from backend.app.models.finding import Finding
from backend.app.models.run import Run

_NODE_EVENT_SUFFIXES = ("_start", "_end")


class IdempotencyKeyConflict(Exception):
    """Raised when an `Idempotency-Key` already recorded on some other run
    is reused with a different `corpus_id`. `runs.idempotency_key` is
    globally unique (migration 001), so silently inserting a second run
    would just trade this for an `IntegrityError`; the normal case (a
    client retrying the same request) always pairs the same key with the
    same corpus_id, so this only ever fires on a client bug."""


async def create_run(
    session: AsyncSession,
    corpus_id: uuid.UUID,
    kind: Literal["initial", "update"],
    idempotency_key: str | None = None,
) -> tuple[Run, bool]:
    """Returns `(run, created)`. If `idempotency_key` matches a run
    already recorded for this corpus, that run is returned with
    `created=False` instead of inserting a duplicate (task_breakdown Step
    24: "if seen for the same corpus, return the existing run")."""
    if idempotency_key:
        existing = await session.scalar(select(Run).where(Run.idempotency_key == idempotency_key))
        if existing is not None:
            if existing.corpus_id != corpus_id:
                raise IdempotencyKeyConflict(
                    f"Idempotency-Key {idempotency_key!r} was already used for corpus "
                    f"{existing.corpus_id}, not {corpus_id}"
                )
            return existing, False

    run = Run(corpus_id=corpus_id, kind=kind, status="pending", idempotency_key=idempotency_key)
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run, True


async def list_runs(session: AsyncSession) -> list[Run]:
    result = await session.scalars(select(Run).order_by(Run.started_at.desc()))
    return list(result.all())


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> Run | None:
    return await session.get(Run, run_id)


async def current_stage(session: AsyncSession, run_id: uuid.UUID) -> str | None:
    """The node name of the most recent `<node>_start`/`<node>_end` audit
    event for this run, or `None` before any node has run. Scans newest
    first rather than taking the single latest row outright, because a
    node's own internal audit writes (e.g. `register_entry_proposed`) can
    sit between its `_start` and `_end` events and would otherwise look
    like the latest event."""
    rows = await session.scalars(
        select(AuditEvent.event_type)
        .where(AuditEvent.run_id == run_id)
        .order_by(AuditEvent.id.desc())
    )
    for event_type in rows:
        for suffix in _NODE_EVENT_SUFFIXES:
            if event_type.endswith(suffix):
                return event_type[: -len(suffix)]
    return None


async def run_counts(session: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    claims = await session.scalar(select(func.count(Claim.id)).where(Claim.run_id == run_id))
    conflicts = await session.scalar(
        select(func.count(Conflict.id)).where(Conflict.run_id == run_id)
    )
    findings = await session.scalar(select(func.count(Finding.id)).where(Finding.run_id == run_id))
    return {"claims": claims or 0, "conflicts": conflicts or 0, "findings": findings or 0}


async def get_run_cost(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(
                CostEvent.stage,
                func.count(CostEvent.id),
                func.coalesce(func.sum(CostEvent.input_tokens), 0),
                func.coalesce(func.sum(CostEvent.output_tokens), 0),
                func.coalesce(func.sum(CostEvent.latency_ms), 0),
                func.coalesce(func.sum(CostEvent.usd_cost), 0),
            )
            .where(CostEvent.run_id == run_id)
            .group_by(CostEvent.stage)
            .order_by(CostEvent.stage)
        )
    ).all()

    stages = [
        {
            "stage": stage,
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "usd_cost": usd_cost,
        }
        for stage, calls, input_tokens, output_tokens, latency_ms, usd_cost in rows
    ]
    total = sum((Decimal(s["usd_cost"]) for s in stages), Decimal(0))
    return {"run_id": run_id, "total_usd_cost": total, "stages": stages}


async def get_run_audit(session: AsyncSession, run_id: uuid.UUID) -> list[AuditEvent]:
    result = await session.scalars(
        select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.id)
    )
    return list(result.all())
