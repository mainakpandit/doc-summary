"""`extract` node (implementation plan 8.2).

For each document in `state.documents`, retrieves that document's chunks,
wraps them via `injection_guard.wrap_sources` (CLAUDE.md behavior 8 -- raw
chunk text is never concatenated into a prompt by hand; this is the first
node that sends chunk text to an LLM), and asks Claude to extract sourced
claims. The system prompt is chosen by `state.classifications[doc.id]`,
one file per doc_type under `prompts/extract_{doc_type}.txt` (falling back
to `extract_other.txt` for a doc_type outside `classify.ALLOWED_DOC_TYPES`,
which shouldn't happen in the normal classify -> extract flow but keeps
this node from crashing on it).

CLAUDE.md behavior 5 ("no bluff"): every claim source's `quote` is checked
against its cited chunk's actual text with a plain `in` substring test. A
claim with any source that fails this check -- unresolvable chunk_id or a
quote that isn't an exact substring -- is dropped whole, not just that
source, and one `claim_rejected_bad_quote` audit event is written per
dropped claim (see test_no_bluff.py). Surviving claims and their
claim_sources are persisted; nothing is invented for missing data.

Also runs `injection_guard.scan_response` on each raw LLM response and
records a `possible_prompt_injection` Finding on a hit (CLAUDE.md behavior
8). A hit drops that response whole and skips the rest of this document --
no claims are parsed from a response that may have been hijacked by
content embedded in the source -- but it never fails the run: the next
document in `state.documents` is still processed, and a human reviews the
finding at the gate. See `test_injection.py` (B8).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from backend.app.agent.nodes.classify import ALLOWED_DOC_TYPES
from backend.app.agent.state import AgentState, ClaimDraft, ClaimSourceDraft
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.chunk import Chunk
from backend.app.models.claim import Claim, ClaimSource
from backend.app.models.finding import Finding
from backend.app.services.injection_guard import scan_response, wrap_sources
from backend.app.services.llm import call_claude

logger = structlog.get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

SYSTEM_PROMPTS: dict[str, str] = {
    doc_type: (PROMPTS_DIR / f"extract_{doc_type}.txt").read_text()
    for doc_type in ALLOWED_DOC_TYPES
}


def build_messages(chunks: list[Chunk]) -> list[dict[str, Any]]:
    wrapped = wrap_sources([(chunk.id, chunk.text) for chunk in chunks])
    return [{"role": "user", "content": wrapped}]


def _parse_response(text: str) -> list[dict[str, Any]]:
    """Parse the raw JSON list Claude returned. Raises on a structurally
    invalid response (not a list, missing keys) -- that's a prompt/response
    contract violation, not a hallucination. Per-source hallucinations (a
    chunk_id that doesn't resolve, a quote that doesn't match) are the
    caller's job via `_verify_sources`, not this function's."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"extract: non-JSON response: {text!r}") from exc

    if not isinstance(parsed, list):
        raise TypeError(f"extract: expected a JSON list, got {type(parsed).__name__}: {text!r}")

    claims: list[dict[str, Any]] = []
    for item in parsed:
        claims.append(
            {
                "subject": str(item["subject"]),
                "predicate": str(item["predicate"]),
                "object": str(item["object"]),
                "confidence": float(item["confidence"]),
                "sources": [
                    {"chunk_id": str(source["chunk_id"]), "quote": str(source["quote"])}
                    for source in item["sources"]
                ],
            }
        )
    return claims


def _verify_sources(
    sources: list[dict[str, str]], chunk_lookup: dict[uuid.UUID, Chunk]
) -> list[ClaimSourceDraft] | None:
    """Every source must resolve to a retrieved chunk and its quote must be
    an exact substring of that chunk's text; a claim with zero sources also
    fails (CLAUDE.md: "Every claim has at least one claim_sources row ...
    No exceptions."). Returns None if any source fails -- the whole claim
    is dropped, not just the bad source."""
    if not sources:
        return None

    verified: list[ClaimSourceDraft] = []
    for source in sources:
        try:
            chunk_id = uuid.UUID(source["chunk_id"])
        except ValueError:
            return None
        chunk = chunk_lookup.get(chunk_id)
        if chunk is None or source["quote"] not in chunk.text:
            return None
        verified.append(ClaimSourceDraft(chunk_id=chunk_id, quote=source["quote"]))
    return verified


async def extract_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="extract")

    claims: list[ClaimDraft] = list(state.claims)
    rejected = 0

    async with AsyncSessionLocal() as session:
        for doc in state.documents:
            chunks = list(
                (
                    await session.scalars(
                        select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.idx)
                    )
                ).all()
            )
            if not chunks:
                continue

            doc_type = state.classifications.get(doc.id, "other")
            system_prompt = SYSTEM_PROMPTS.get(doc_type, SYSTEM_PROMPTS["other"])
            chunk_lookup = {chunk.id: chunk for chunk in chunks}

            response = await call_claude(
                session,
                state.run_id,
                stage="extract",
                system=system_prompt,
                messages=build_messages(chunks),
            )

            smells = scan_response(response.text)
            if smells:
                session.add(
                    Finding(
                        run_id=state.run_id,
                        rule_id="possible_prompt_injection",
                        severity="warning",
                        subject=str(doc.id),
                        message=(
                            "extract: possible prompt injection smell in LLM response "
                            f"for document {doc.id}: "
                            f"{[(s.category, s.excerpt) for s in smells]}"
                        ),
                    )
                )
                # Drop the response whole and skip this document -- a
                # response that trips a smell can't be trusted enough to
                # parse claims out of, but one poisoned document must not
                # fail the whole run (CLAUDE.md behavior 8).
                continue

            for parsed in _parse_response(response.text):
                verified_sources = _verify_sources(parsed["sources"], chunk_lookup)
                if verified_sources is None:
                    rejected += 1
                    session.add(
                        AuditEvent(
                            run_id=state.run_id,
                            event_type="claim_rejected_bad_quote",
                            payload={
                                "document_id": str(doc.id),
                                "subject": parsed["subject"],
                                "predicate": parsed["predicate"],
                                "object": parsed["object"],
                                "sources": parsed["sources"],
                            },
                        )
                    )
                    continue

                claim = Claim(
                    run_id=state.run_id,
                    corpus_id=state.corpus_id,
                    subject=parsed["subject"],
                    predicate=parsed["predicate"],
                    object=parsed["object"],
                    confidence=parsed["confidence"],
                )
                session.add(claim)
                await session.flush()  # assign claim.id for claim_sources' FK

                for source in verified_sources:
                    session.add(
                        ClaimSource(claim_id=claim.id, chunk_id=source.chunk_id, quote=source.quote)
                    )

                claims.append(
                    ClaimDraft(
                        id=claim.id,
                        subject=parsed["subject"],
                        predicate=parsed["predicate"],
                        object=parsed["object"],
                        confidence=parsed["confidence"],
                        sources=verified_sources,
                    )
                )

        await session.commit()

    logger.info(
        "agent_node_exit",
        run_id=str(state.run_id),
        node="extract",
        claims=len(claims),
        rejected=rejected,
    )
    return {"claims": claims}
