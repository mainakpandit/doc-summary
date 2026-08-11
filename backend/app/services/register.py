"""`GET /corpora/{id}/register` (Register page). For each committed
`register_entries` row, groups its `register_field_sources` by `field_name`
and resolves each backing claim (predicate/object/confidence) plus that
claim's citations via `services/citations.py` -- CLAUDE.md behavior 5
("every claim carries a chunk_id and a verbatim quote"), and the same
citation shape `services/review.py` already uses so the frontend needs only
one `Citation` type.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.claim import Claim
from backend.app.models.register_entry import RegisterEntry
from backend.app.services.citations import claim_citations


@dataclass
class RegisterEntryData:
    id: uuid.UUID
    feature_key: str
    fields: dict[str, Any]
    field_claims: dict[str, list[dict[str, Any]]]
    version: int
    updated_at: datetime | None


async def _field_claims(
    session: AsyncSession, entry: RegisterEntry
) -> dict[str, list[dict[str, Any]]]:
    claim_ids_by_field: dict[str, list[uuid.UUID]] = defaultdict(list)
    for source in entry.field_sources:
        claim_ids_by_field[source.field_name].append(source.claim_id)

    field_claims: dict[str, list[dict[str, Any]]] = {}
    for field_name, claim_ids in claim_ids_by_field.items():
        claims: list[dict[str, Any]] = []
        for claim_id in claim_ids:
            claim = await session.get(Claim, claim_id)
            if claim is None:
                continue
            claims.append(
                {
                    "claim_id": str(claim.id),
                    "predicate": claim.predicate,
                    "object": claim.object,
                    "confidence": claim.confidence,
                    "sources": await claim_citations(session, claim.id),
                }
            )
        field_claims[field_name] = claims
    return field_claims


async def list_register_entries(
    session: AsyncSession, corpus_id: uuid.UUID
) -> list[RegisterEntryData]:
    entries = (
        await session.scalars(
            select(RegisterEntry)
            .where(RegisterEntry.corpus_id == corpus_id)
            .order_by(RegisterEntry.feature_key)
        )
    ).all()

    return [
        RegisterEntryData(
            id=entry.id,
            feature_key=entry.feature_key,
            fields=entry.fields,
            field_claims=await _field_claims(session, entry),
            version=entry.version,
            updated_at=entry.updated_at,
        )
        for entry in entries
    ]
