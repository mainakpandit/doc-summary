"""`POST /corpora/{id}/documents` (task_breakdown Step 24): accepts an
upload, saves the raw bytes under `CORPUS_ROOT/<corpus_id>/<sha256>.<ext>`
(`services.ingestion.save_upload`), then parses/chunks/persists it
(`services.ingestion.ingest_file`)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.db import get_session
from backend.app.models.corpus import Corpus
from backend.app.schemas.document import DocumentRead
from backend.app.services.ingestion import ingest_file, save_upload
from backend.app.services.parsers import UnsupportedFormat

router = APIRouter(prefix="/corpora", tags=["documents"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/{corpus_id}/documents", response_model=DocumentRead, status_code=201)
async def upload_document(
    corpus_id: uuid.UUID,
    file: UploadFile,
    session: SessionDep,
) -> DocumentRead:
    corpus = await session.get(Corpus, corpus_id)
    if corpus is None:
        raise HTTPException(status_code=404, detail=f"corpus {corpus_id} not found")

    content = await file.read()
    path = save_upload(corpus_id, file.filename or "upload", content)

    try:
        document = await ingest_file(session, path, corpus_id)
    except UnsupportedFormat as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return DocumentRead.model_validate(document)
