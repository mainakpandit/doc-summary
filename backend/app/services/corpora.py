"""Corpus creation and listing -- the service layer `POST`/`GET /corpora`
call, and the same functions the future MCP server's
`create_corpus`/`list_corpora` tools will call (CLAUDE.md: "HTTP and MCP
call the same service layer; no logic in route handlers.").
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.corpus import Corpus


async def create_corpus(
    session: AsyncSession, name: str, inbox_path: str, rules_path: str | None = None
) -> Corpus:
    corpus = Corpus(name=name, inbox_path=inbox_path, rules_path=rules_path)
    session.add(corpus)
    await session.commit()
    await session.refresh(corpus)
    return corpus


async def list_corpora(session: AsyncSession) -> list[Corpus]:
    result = await session.scalars(select(Corpus).order_by(Corpus.created_at.desc()))
    return list(result.all())
