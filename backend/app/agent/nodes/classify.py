"""`classify` node (implementation plan 8.1).

Groups `state.documents` into batches of three and asks Claude (system
prompt in `prompts/classify.txt`) to assign each one a `doc_type`. Results
land in two places: `state.classifications` (for the rest of the graph)
and `documents.doc_type` (persisted). A classification with
`confidence < CONFIDENCE_ESCALATION_THRESHOLD` doesn't fail the run, but it
does write a `classify_escalated` audit event and flips
`state.needs_classification_review`, which `graph.py` uses to route to
`classify_review` instead of straight to `finish`.

Only `document_id` and `filename` are ever sent to the model -- `DocumentRef`
carries nothing else, and chunk text is deliberately not fetched for this
stage (see docs/assumptions.md: filenames are treated as low-risk metadata,
not the "source text" CLAUDE.md behavior 8 requires wrapping via
`injection_guard.wrap_sources`, which doesn't exist yet as of this step).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import structlog

from backend.app.agent.state import AgentState, DocumentRef
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.document import Document
from backend.app.services.llm import call_claude

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "classify.txt").read_text()

BATCH_SIZE = 3
CONFIDENCE_ESCALATION_THRESHOLD = 0.5

ALLOWED_DOC_TYPES = {
    "prd",
    "techspec",
    "ticket_export",
    "meeting_notes",
    "release_notes",
    "postmortem",
    "other",
}


def _batches(documents: list[DocumentRef], size: int) -> list[list[DocumentRef]]:
    return [documents[i : i + size] for i in range(0, len(documents), size)]


def build_batch_messages(batch: list[DocumentRef]) -> list[dict[str, Any]]:
    payload = [{"document_id": str(doc.id), "filename": doc.filename} for doc in batch]
    return [{"role": "user", "content": json.dumps(payload)}]


def _parse_classifications(text: str, batch: list[DocumentRef]) -> list[dict[str, Any]]:
    batch_ids = {doc.id for doc in batch}

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"classify: non-JSON response for batch {batch_ids}: {text!r}") from exc

    if not isinstance(parsed, list):
        raise TypeError(f"classify: expected a JSON list, got {type(parsed).__name__}: {text!r}")

    results: list[dict[str, Any]] = []
    seen_ids: set[uuid.UUID] = set()
    for item in parsed:
        doc_id = uuid.UUID(str(item["document_id"]))
        doc_type = item["doc_type"]
        confidence = float(item["confidence"])

        if doc_id not in batch_ids:
            raise ValueError(f"classify: document_id {doc_id} was not in the requested batch")
        if doc_type not in ALLOWED_DOC_TYPES:
            raise ValueError(
                f"classify: doc_type {doc_type!r} is not one of {sorted(ALLOWED_DOC_TYPES)}"
            )

        seen_ids.add(doc_id)
        results.append({"document_id": doc_id, "doc_type": doc_type, "confidence": confidence})

    missing = batch_ids - seen_ids
    if missing:
        raise ValueError(f"classify: response is missing classifications for {missing}")

    return results


async def classify_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="classify")

    classifications: dict[uuid.UUID, str] = dict(state.classifications)
    escalated: list[dict[str, Any]] = []

    async with AsyncSessionLocal() as session:
        for batch in _batches(state.documents, BATCH_SIZE):
            response = await call_claude(
                session,
                state.run_id,
                stage="classify",
                system=SYSTEM_PROMPT,
                messages=build_batch_messages(batch),
            )

            for result in _parse_classifications(response.text, batch):
                doc_id, doc_type, confidence = (
                    result["document_id"],
                    result["doc_type"],
                    result["confidence"],
                )
                classifications[doc_id] = doc_type

                document = await session.get(Document, doc_id)
                if document is not None:
                    document.doc_type = doc_type

                if confidence < CONFIDENCE_ESCALATION_THRESHOLD:
                    escalated.append(
                        {
                            "document_id": str(doc_id),
                            "doc_type": doc_type,
                            "confidence": confidence,
                        }
                    )

        if escalated:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="classify_escalated",
                    payload={"documents": escalated},
                )
            )

        await session.commit()

    logger.info(
        "agent_node_exit",
        run_id=str(state.run_id),
        node="classify",
        classified=len(classifications),
        escalated=len(escalated),
    )
    return {
        "classifications": classifications,
        "needs_classification_review": bool(escalated),
    }


async def classify_review_node(state: AgentState) -> dict[str, Any]:
    """Stub: full human review of individual classifications is out of MVP
    scope (docs/assumptions.md), matching CLAUDE.md's "no per-claim human
    review in the UI for MVP" cut. This just records that the run passed
    through here so the escalation isn't silently dropped, then the graph
    falls through to `finish` same as the non-escalated path."""
    logger.info("agent_node_enter", run_id=str(state.run_id), node="classify_review")

    async with AsyncSessionLocal() as session:
        session.add(
            AuditEvent(
                run_id=state.run_id,
                event_type="classify_review_noted",
                payload={
                    "note": "stub: per-classification human review is out of MVP scope, "
                    "see docs/assumptions.md"
                },
            )
        )
        await session.commit()

    logger.info("agent_node_exit", run_id=str(state.run_id), node="classify_review")
    return {}
