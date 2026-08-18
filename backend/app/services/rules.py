"""Rules playbook loader and evaluator (`examine` node, plan 8.4).

A corpus's rules live in a YAML file at `Corpus.rules_path` (resolved
against `settings.CORPUS_ROOT` by the caller); full schema documented in
`backend/app/agent/rules/schema.md`. Each `Rule` has exactly one
evaluator:

- `deterministic`: a three-verb predicate DSL evaluated in Python against
  this run's claims, grouped by `subject` ("feature"). No LLM call, no
  cost.
- `llm`: a prompt template (`backend/app/agent/prompts/<file>`) run
  through `call_claude` with the rule's selected claims, returning one
  boolean verdict per subject in the selection.

`evaluate_rules` is the single entrypoint `agent/nodes/examine.py` calls.
It returns `Violation`s, not `Finding` rows -- persisting those (and their
`finding_sources`) stays the node's job, same division of labor as
`detect_conflicts` (SQL builds rows, the node writes them).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Literal

import structlog
import yaml
from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.state import ClaimDraft
from backend.app.services.injection_guard import Smell, scan_response, wrap_sources
from backend.app.services.llm import call_claude

logger = structlog.get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "agent" / "prompts"


class DeterministicSpec(BaseModel):
    op: Literal["every_subject_has", "no_subject_has", "at_least_one"]
    predicate: str
    value: str | None = None

    @model_validator(mode="after")
    def _value_required_for_no_subject_has(self) -> DeterministicSpec:
        if self.op == "no_subject_has" and self.value is None:
            raise ValueError("deterministic.value is required when op is 'no_subject_has'")
        return self


class LLMSpec(BaseModel):
    select_predicate: str
    select_object: str | None = None
    prompt: str


class Rule(BaseModel):
    id: str
    description: str
    severity: Literal["info", "warning", "error"]
    deterministic: DeterministicSpec | None = None
    llm: LLMSpec | None = None

    @model_validator(mode="after")
    def _exactly_one_evaluator(self) -> Rule:
        if (self.deterministic is None) == (self.llm is None):
            raise ValueError(f"rule {self.id!r} must set exactly one of 'deterministic' or 'llm'")
        return self


class Violation(BaseModel):
    rule_id: str
    severity: Literal["info", "warning", "error"]
    subject: str | None
    message: str
    claim_ids: list[uuid.UUID] = []


class EvaluationResult(BaseModel):
    violations: list[Violation] = []
    injection_smells: list[Smell] = []


def load_rules(path: Path) -> list[Rule]:
    raw = yaml.safe_load(path.read_text())
    entries = (raw or {}).get("rules", [])
    return [Rule.model_validate(entry) for entry in entries]


def _by_subject(claims: list[ClaimDraft]) -> dict[str, list[ClaimDraft]]:
    grouped: dict[str, list[ClaimDraft]] = {}
    for claim in claims:
        grouped.setdefault(claim.subject, []).append(claim)
    return grouped


def _evaluate_deterministic(rule: Rule, claims: list[ClaimDraft]) -> list[Violation]:
    spec = rule.deterministic
    assert spec is not None
    grouped = _by_subject(claims)
    violations: list[Violation] = []

    if spec.op == "every_subject_has":
        for subject, subject_claims in grouped.items():
            if not any(c.predicate == spec.predicate for c in subject_claims):
                violations.append(
                    Violation(
                        rule_id=rule.id,
                        severity=rule.severity,
                        subject=subject,
                        message=f"{subject!r} has no claim for predicate {spec.predicate!r}",
                        claim_ids=[c.id for c in subject_claims if c.id is not None],
                    )
                )

    elif spec.op == "no_subject_has":
        for subject, subject_claims in grouped.items():
            matches = [
                c
                for c in subject_claims
                if c.predicate == spec.predicate and c.object == spec.value
            ]
            if matches:
                violations.append(
                    Violation(
                        rule_id=rule.id,
                        severity=rule.severity,
                        subject=subject,
                        message=(
                            f"{subject!r} has {spec.predicate}={spec.value!r}, "
                            f"which rule {rule.id!r} forbids"
                        ),
                        claim_ids=[c.id for c in matches if c.id is not None],
                    )
                )

    elif spec.op == "at_least_one" and not any(c.predicate == spec.predicate for c in claims):
        violations.append(
            Violation(
                rule_id=rule.id,
                severity=rule.severity,
                subject=None,
                message=f"no claim with predicate {spec.predicate!r} found",
            )
        )

    return violations


def select_subjects_for_llm_rule(
    rule: Rule, claims: list[ClaimDraft]
) -> dict[str, list[ClaimDraft]]:
    """The subset of `_by_subject(claims)` whose subject has at least one
    claim matching the rule's `select_predicate`/`select_object`. Public
    (not `_`-prefixed) so tests can build the exact selection `examine`
    will send an LLM without duplicating this filter."""
    spec = rule.llm
    assert spec is not None
    grouped = _by_subject(claims)
    return {
        subject: subject_claims
        for subject, subject_claims in grouped.items()
        if any(
            c.predicate == spec.select_predicate
            and (spec.select_object is None or c.object == spec.select_object)
            for c in subject_claims
        )
    }


def build_llm_messages(
    selected: dict[str, list[ClaimDraft]], doc_types_by_claim: dict[uuid.UUID, list[str]]
) -> list[dict[str, Any]]:
    """The exact message list `examine` sends for one `llm` rule, given its
    subject selection. Public for the same reason as `extract.build_messages`
    -- tests seed `FakeLLM` by calling this instead of hand-computing a
    cache-key hash."""
    sources: list[tuple[uuid.UUID, str]] = []
    evidence: list[dict[str, Any]] = []
    for subject in sorted(selected):
        for claim in selected[subject]:
            for source in claim.sources:
                sources.append((source.chunk_id, source.quote))
            evidence.append(
                {
                    "subject": claim.subject,
                    "predicate": claim.predicate,
                    "object": claim.object,
                    "claim_id": str(claim.id),
                    "source_doc_types": (
                        sorted(doc_types_by_claim.get(claim.id, [])) if claim.id is not None else []
                    ),
                }
            )

    content = (
        "Claims for the features to check (JSON list):\n"
        f"{json.dumps(evidence)}\n\n"
        "Verbatim source quotes backing those claims:\n"
        f"{wrap_sources(sources)}"
    )
    return [{"role": "user", "content": content}]


def _parse_llm_verdicts(text: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"examine: non-JSON LLM verdict response: {text!r}") from exc
    if not isinstance(parsed, list):
        raise TypeError(f"examine: expected a JSON list, got {type(parsed).__name__}: {text!r}")
    return parsed


async def _evaluate_llm(
    session: AsyncSession,
    run_id: uuid.UUID,
    rule: Rule,
    claims: list[ClaimDraft],
    doc_types_by_claim: dict[uuid.UUID, list[str]],
) -> tuple[list[Violation], list[Smell]]:
    spec = rule.llm
    assert spec is not None

    selected = select_subjects_for_llm_rule(rule, claims)
    if not selected:
        return [], []

    system_prompt = (PROMPTS_DIR / spec.prompt).read_text()
    messages = build_llm_messages(selected, doc_types_by_claim)

    response = await call_claude(
        session, run_id, stage="examine", system=system_prompt, messages=messages
    )

    smells = scan_response(response.text)
    if smells:
        # A smell means this response can't be trusted -- drop it whole
        # rather than parsing verdicts out of it (CLAUDE.md behavior 8:
        # a hit is a Finding for a human, never a silent side effect, but
        # it also must never let a hijacked response's verdicts through).
        return [], smells

    violations: list[Violation] = []
    for verdict in _parse_llm_verdicts(response.text):
        subject = str(verdict["subject"])
        if subject not in selected or bool(verdict["result"]):
            continue
        violations.append(
            Violation(
                rule_id=rule.id,
                severity=rule.severity,
                subject=subject,
                message=str(verdict.get("reason") or f"rule {rule.id!r} failed for {subject!r}"),
                claim_ids=[c.id for c in selected[subject] if c.id is not None],
            )
        )

    return violations, []


async def evaluate_rules(
    session: AsyncSession,
    run_id: uuid.UUID,
    rules: list[Rule],
    claims: list[ClaimDraft],
    doc_types_by_claim: dict[uuid.UUID, list[str]],
) -> EvaluationResult:
    result = EvaluationResult()
    for rule in rules:
        if rule.deterministic is not None:
            result.violations.extend(_evaluate_deterministic(rule, claims))
        else:
            violations, smells = await _evaluate_llm(
                session, run_id, rule, claims, doc_types_by_claim
            )
            result.violations.extend(violations)
            result.injection_smells.extend(smells)
    return result
