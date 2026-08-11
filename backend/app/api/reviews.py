"""`GET`/`POST /runs/{id}/review`, `POST /runs/{id}/resume` -- the human
gate API (task_breakdown Step 25, behavior 3). Route handlers stay thin per
CLAUDE.md: payload-building and decision-application logic lives in
`services/review.py`, and driving the graph lives in `agent/graph.py`
(`resume_run`, `get_agent_state`) -- this module only translates HTTP to
those two.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.graph import get_agent_state, resume_run
from backend.app.db import get_session
from backend.app.models.run import Run
from backend.app.schemas.review import (
    ResumeResult,
    ReviewPayloadRead,
    ReviewSubmission,
    ReviewSubmissionResult,
)
from backend.app.services import review as review_service

router = APIRouter(prefix="/runs", tags=["reviews"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _get_run_or_404(session: AsyncSession, run_id: uuid.UUID) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return run


@router.get("/{run_id}/review", response_model=ReviewPayloadRead)
async def get_review(run_id: uuid.UUID, session: SessionDep) -> ReviewPayloadRead:
    run = await _get_run_or_404(session, run_id)
    state = await get_agent_state(run_id)
    register_diff = state.register_diff if state else None

    payload = await review_service.build_review_payload(session, run_id, register_diff)
    return ReviewPayloadRead(
        run_id=run_id,
        status=run.status,
        conflicts=payload.conflicts,
        findings=payload.findings,
        register_changes=payload.register_changes,
    )


@router.post("/{run_id}/review", response_model=ReviewSubmissionResult)
async def submit_review(
    run_id: uuid.UUID, body: ReviewSubmission, session: SessionDep
) -> ReviewSubmissionResult:
    run = await _get_run_or_404(session, run_id)
    if run.status != "awaiting_review":
        raise HTTPException(
            status_code=409, detail=f"run {run_id} is not awaiting review (status={run.status})"
        )

    state = await get_agent_state(run_id)
    register_diff = state.register_diff if state else None
    payload = await review_service.build_review_payload(session, run_id, register_diff)
    valid_types = {item["id"]: item["item_type"] for item in payload.conflicts}
    valid_types.update({item["id"]: item["item_type"] for item in payload.findings})
    valid_types.update({item["id"]: item["item_type"] for item in payload.register_changes})

    for item in body.items:
        item_id = str(item.id)
        if valid_types.get(item_id) != item.item_type:
            raise HTTPException(
                status_code=404,
                detail=f"{item.item_type} {item.id} is not a pending review item for run {run_id}",
            )

    try:
        await review_service.submit_review_decisions(session, run_id, body.items, body.reviewer)
    except review_service.UnknownReviewItemError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return ReviewSubmissionResult(accepted=len(body.items))


@router.post("/{run_id}/resume", response_model=ResumeResult)
async def resume(run_id: uuid.UUID, session: SessionDep) -> ResumeResult:
    run = await _get_run_or_404(session, run_id)
    if run.status != "awaiting_review":
        raise HTTPException(
            status_code=409, detail=f"run {run_id} is not awaiting review (status={run.status})"
        )

    await resume_run(run_id)

    await session.refresh(run)
    return ResumeResult(run_id=run_id, status=run.status)
