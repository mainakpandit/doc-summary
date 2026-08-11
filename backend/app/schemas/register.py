"""Response model for `GET /corpora/{id}/register` (`api/register.py`,
frontend Register page). `sources` reuses the same loose `dict[str, Any]`
citation shape `schemas/review.py` already exposes over HTTP.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class FieldClaimRead(BaseModel):
    claim_id: uuid.UUID
    predicate: str
    object: str
    confidence: float
    sources: list[dict[str, Any]]


class RegisterEntryRead(BaseModel):
    id: uuid.UUID
    feature_key: str
    fields: dict[str, Any]
    field_claims: dict[str, list[FieldClaimRead]]
    version: int
    updated_at: datetime | None
