"""Response model for `POST /corpora/{id}/documents` (task_breakdown Step
24)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    corpus_id: uuid.UUID
    filename: str
    content_hash: str
    mime_type: str
    doc_type: str | None
    ingested_at: datetime | None


class DocumentTextRead(BaseModel):
    document_id: uuid.UUID
    filename: str
    text: str
