"""Request/response models for `POST`/`GET /corpora` (task_breakdown Step
24)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CorpusCreate(BaseModel):
    name: str
    inbox_path: str
    rules_path: str | None = None


class CorpusRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    inbox_path: str
    rules_path: str | None
    created_at: datetime | None
