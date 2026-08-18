"""`POST`/`GET /corpora` (task_breakdown Step 24). Thin: all persistence
logic lives in `services/corpora.py` so a future MCP server tool can call
the same functions (CLAUDE.md: "HTTP and MCP call the same service
layer")."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import get_session
from backend.app.schemas.corpus import CorpusCreate, CorpusRead
from backend.app.services import corpora as corpora_service

router = APIRouter(prefix="/corpora", tags=["corpora"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("", response_model=CorpusRead, status_code=201)
async def create_corpus(body: CorpusCreate, session: SessionDep) -> CorpusRead:
    corpus = await corpora_service.create_corpus(
        session, body.name, body.inbox_path, body.rules_path
    )
    return CorpusRead.model_validate(corpus)


@router.get("", response_model=list[CorpusRead])
async def list_corpora(session: SessionDep) -> list[CorpusRead]:
    corpora = await corpora_service.list_corpora(session)
    return [CorpusRead.model_validate(c) for c in corpora]
