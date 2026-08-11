"""Request/response models for the `/runs` endpoints (task_breakdown Step
24)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class RunCreate(BaseModel):
    corpus_id: uuid.UUID
    kind: Literal["initial", "update"]


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    corpus_id: uuid.UUID
    kind: str
    status: str
    parent_run_id: uuid.UUID | None
    triggering_document_id: uuid.UUID | None
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    idempotency_key: str | None


class RunDetail(RunRead):
    current_stage: str | None
    counts: dict[str, int]


class CostStage(BaseModel):
    stage: str
    calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: int
    usd_cost: float


class RunCost(BaseModel):
    run_id: uuid.UUID
    total_usd_cost: float
    stages: list[CostStage]


class AuditEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: uuid.UUID | None
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime | None
