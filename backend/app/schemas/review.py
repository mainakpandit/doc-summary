"""Request/response models for the review-gate endpoints (`api/reviews.py`,
task_breakdown Step 25). `ReviewItemDecision` is also the type
`services/review.submit_review_decisions` accepts directly -- it carries no
FastAPI-specific behavior, so a future MCP `submit_review` tool can
construct the same instances without going through HTTP (CLAUDE.md
behavior 4: HTTP and MCP share one service layer).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel


class ReviewItemDecision(BaseModel):
    id: uuid.UUID
    item_type: Literal["conflict", "finding", "register_change"]
    decision: Literal["approve", "reject"]
    note: str | None = None


class ReviewSubmission(BaseModel):
    items: list[ReviewItemDecision]
    reviewer: str


class ReviewSubmissionResult(BaseModel):
    accepted: int


class ReviewPayloadRead(BaseModel):
    run_id: uuid.UUID
    status: str
    conflicts: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    register_changes: list[dict[str, Any]]


class ResumeResult(BaseModel):
    run_id: uuid.UUID
    status: str
