"""`GET /corpora/{id}/register` -- the Feature Register list (frontend
Register page). Thin per CLAUDE.md: entry/claim/citation resolution lives
in `services/register.py`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import get_session
from backend.app.models.corpus import Corpus
from backend.app.schemas.register import RegisterEntryRead
from backend.app.services import register as register_service

router = APIRouter(prefix="/corpora", tags=["register"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/{corpus_id}/register", response_model=list[RegisterEntryRead])
async def list_register(corpus_id: uuid.UUID, session: SessionDep) -> list[RegisterEntryRead]:
    corpus = await session.get(Corpus, corpus_id)
    if corpus is None:
        raise HTTPException(status_code=404, detail=f"corpus {corpus_id} not found")

    entries = await register_service.list_register_entries(session, corpus_id)
    return [RegisterEntryRead.model_validate(entry, from_attributes=True) for entry in entries]
