"""`POST`/`GET /runs`, `GET /runs/{id}`, `/cost`, `/audit`, `/events`
(task_breakdown Step 24). Every agent node's entry/exit SSE event is
published by `agent/instrumentation.py` through `services/events.py`;
this route only replays/streams what's already there, it never drives the
graph itself -- `backend/app/worker.py` (a separate process, per
CLAUDE.md's `make dev`) is what actually runs a pending `Run`.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from backend.app.db import get_session
from backend.app.models.corpus import Corpus
from backend.app.schemas.run import AuditEventRead, RunCost, RunCreate, RunDetail, RunRead
from backend.app.services import runs as runs_service
from backend.app.services.events import stream_run_events

router = APIRouter(prefix="/runs", tags=["runs"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKeyDep = Annotated[str | None, Header(alias="Idempotency-Key")]


@router.post("", response_model=RunRead, status_code=201)
async def create_run(
    body: RunCreate, session: SessionDep, idempotency_key: IdempotencyKeyDep = None
) -> RunRead:
    corpus = await session.get(Corpus, body.corpus_id)
    if corpus is None:
        raise HTTPException(status_code=404, detail=f"corpus {body.corpus_id} not found")

    try:
        run, _created = await runs_service.create_run(
            session, body.corpus_id, body.kind, idempotency_key
        )
    except runs_service.IdempotencyKeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return RunRead.model_validate(run)


@router.get("", response_model=list[RunRead])
async def list_runs(session: SessionDep) -> list[RunRead]:
    runs = await runs_service.list_runs(session)
    return [RunRead.model_validate(r) for r in runs]


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(run_id: uuid.UUID, session: SessionDep) -> RunDetail:
    run = await runs_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    stage = await runs_service.current_stage(session, run_id)
    counts = await runs_service.run_counts(session, run_id)
    return RunDetail(**RunRead.model_validate(run).model_dump(), current_stage=stage, counts=counts)


@router.get("/{run_id}/cost", response_model=RunCost)
async def get_run_cost(run_id: uuid.UUID, session: SessionDep) -> RunCost:
    run = await runs_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    return RunCost(**await runs_service.get_run_cost(session, run_id))


@router.get("/{run_id}/audit", response_model=list[AuditEventRead])
async def get_run_audit(run_id: uuid.UUID, session: SessionDep) -> list[AuditEventRead]:
    run = await runs_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    events = await runs_service.get_run_audit(session, run_id)
    return [AuditEventRead.model_validate(e) for e in events]


@router.get("/{run_id}/events")
async def stream_events(run_id: uuid.UUID, session: SessionDep) -> EventSourceResponse:
    run = await runs_service.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    async def generator():
        async for event in stream_run_events(run_id):
            yield {"event": event["event_type"], "data": json.dumps(event["payload"], default=str)}

    return EventSourceResponse(generator())
