"""`examine` node (implementation plan 8.4).

Loads this run's corpus rules playbook (`Corpus.rules_path`, resolved
against `settings.CORPUS_ROOT`; schema at
`backend/app/agent/rules/schema.md`) and evaluates every rule against
`state.claims` via `services.rules.evaluate_rules`. Each violation becomes
one `findings` row plus one `finding_sources` row per backing claim (a
violation that inherently can't cite one, e.g. `at_least_one` against zero
matching claims, gets none -- CLAUDE.md behavior 5, never invent a
source), and one `examine_finding` audit event.

A corpus with no `rules_path` set has nothing to check and, like a corpus
whose rules all pass, writes a single `examine_clean` audit event instead
-- the entry/exit log lines already cover "did this node run"; this one
specifically says "it ran and nothing was wrong."

A `possible_prompt_injection` finding (CLAUDE.md behavior 8) is written
for any `llm`-evaluator rule whose raw response trips
`injection_guard.scan_response`, same as `extract.py`; it never changes
what a rule concludes, only adds a finding a human reviews.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.state import AgentState, FindingDraft
from backend.app.config import get_settings
from backend.app.db import AsyncSessionLocal
from backend.app.models.audit import AuditEvent
from backend.app.models.chunk import Chunk
from backend.app.models.claim import ClaimSource
from backend.app.models.corpus import Corpus
from backend.app.models.document import Document
from backend.app.models.finding import Finding, finding_sources
from backend.app.services.rules import Rule, Violation, evaluate_rules, load_rules

logger = structlog.get_logger(__name__)


async def _load_rules_for_corpus(session: AsyncSession, corpus_id: uuid.UUID) -> list[Rule]:
    corpus = await session.get(Corpus, corpus_id)
    if corpus is None or not corpus.rules_path:
        return []
    settings = get_settings()
    return load_rules(settings.CORPUS_ROOT / corpus.rules_path)


async def _doc_types_by_claim(
    session: AsyncSession, claim_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    if not claim_ids:
        return {}
    rows = (
        await session.execute(
            select(ClaimSource.claim_id, Document.doc_type)
            .join(Chunk, Chunk.id == ClaimSource.chunk_id)
            .join(Document, Document.id == Chunk.document_id)
            .where(ClaimSource.claim_id.in_(claim_ids))
        )
    ).all()
    grouped: dict[uuid.UUID, list[str]] = {}
    for claim_id, doc_type in rows:
        if doc_type:
            grouped.setdefault(claim_id, []).append(doc_type)
    return grouped


async def _persist_violation(
    session: AsyncSession, run_id: uuid.UUID, violation: Violation
) -> FindingDraft:
    finding = Finding(
        run_id=run_id,
        rule_id=violation.rule_id,
        severity=violation.severity,
        subject=violation.subject,
        message=violation.message,
    )
    session.add(finding)
    await session.flush()  # assign finding.id for finding_sources' FK

    for claim_id in violation.claim_ids:
        await session.execute(
            insert(finding_sources).values(finding_id=finding.id, claim_id=claim_id)
        )

    session.add(
        AuditEvent(
            run_id=run_id,
            event_type="examine_finding",
            payload={
                "finding_id": str(finding.id),
                "rule_id": violation.rule_id,
                "severity": violation.severity,
                "subject": violation.subject,
            },
        )
    )

    return FindingDraft(
        id=finding.id,
        rule_id=violation.rule_id,
        severity=violation.severity,
        subject=violation.subject,
        message=violation.message,
    )


async def examine_node(state: AgentState) -> dict[str, Any]:
    logger.info("agent_node_enter", run_id=str(state.run_id), node="examine")

    findings: list[FindingDraft] = list(state.findings)

    async with AsyncSessionLocal() as session:
        rules = await _load_rules_for_corpus(session, state.corpus_id)

        violations: list[Violation] = []
        if rules:
            claim_ids = [c.id for c in state.claims if c.id is not None]
            doc_types_by_claim = await _doc_types_by_claim(session, claim_ids)
            result = await evaluate_rules(
                session, state.run_id, rules, state.claims, doc_types_by_claim
            )
            violations = result.violations

            for smell in result.injection_smells:
                session.add(
                    Finding(
                        run_id=state.run_id,
                        rule_id="possible_prompt_injection",
                        severity="warning",
                        subject=None,
                        message=f"examine: possible prompt injection smell in LLM response: {smell}",
                    )
                )

        for violation in violations:
            findings.append(await _persist_violation(session, state.run_id, violation))

        if not violations:
            session.add(
                AuditEvent(
                    run_id=state.run_id,
                    event_type="examine_clean",
                    payload={"rules_evaluated": [rule.id for rule in rules]},
                )
            )

        await session.commit()

    logger.info(
        "agent_node_exit", run_id=str(state.run_id), node="examine", findings=len(violations)
    )
    return {"findings": findings}
