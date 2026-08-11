"""Shared graph state (implementation plan section 8).

`AgentState` is the one Pydantic object that flows through every node in
`backend/app/agent/graph.py`. LangGraph checkpoints it after each node via
`AsyncPostgresSaver`, which is what lets a killed-and-restarted worker
resume mid-run instead of re-running completed nodes (CLAUDE.md
behavior 2; see `backend/tests/test_agent_resume.py`).

The nested draft types below aren't populated yet -- only the `start` and
`finish` placeholder nodes exist so far (step 17). They're shaped to match
what the later nodes described in the plan will read and write:
`classify` (8.1), `extract` (8.2), `detect_conflicts` (8.3), `examine`
(8.4), `build_register` / `update` (8.5), `human_gate` (8.6).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel


class DocumentRef(BaseModel):
    """A document in scope for this run."""

    id: uuid.UUID
    filename: str


class ClaimSourceDraft(BaseModel):
    """One citation backing a ClaimDraft -- a verbatim quote from a chunk."""

    chunk_id: uuid.UUID
    quote: str


class ClaimDraft(BaseModel):
    """A sourced fact extracted by the `extract` node (8.2). `id` is set
    once the claim survives quote verification and is persisted."""

    id: uuid.UUID | None = None
    subject: str
    predicate: str
    object: str
    confidence: float
    sources: list[ClaimSourceDraft] = []


class ConflictDraft(BaseModel):
    """Two claims about the same (subject, predicate) with different
    objects, produced by `detect_conflicts` (8.3)."""

    id: uuid.UUID | None = None
    subject: str
    predicate: str
    claim_a_id: uuid.UUID
    claim_b_id: uuid.UUID
    resolution: Literal["unresolved", "kept_a", "kept_b", "kept_both", "rejected_both"] = (
        "unresolved"
    )


class FindingDraft(BaseModel):
    """A rules-playbook violation produced by `examine` (8.4)."""

    id: uuid.UUID | None = None
    rule_id: str
    severity: Literal["info", "warning", "error"]
    subject: str | None = None
    message: str
    status: Literal["pending", "approved", "rejected"] = "pending"


class RegisterEntryDraft(BaseModel):
    """A proposed new `register_entries` row (initial runs only)."""

    feature_key: str
    fields: dict[str, Any]


class RegisterFieldChange(BaseModel):
    """One field of one existing register entry that an update run would
    change, and the claim that causes the change (8.5)."""

    feature_key: str
    field_name: str
    old_value: Any | None = None
    new_value: Any | None = None
    claim_id: uuid.UUID


class RegisterDiff(BaseModel):
    """Output of `build_register` / `update` (8.5). `unaffected` lists the
    feature_keys of existing entries this run proves it did not need to
    touch."""

    additions: list[RegisterEntryDraft] = []
    changes: list[RegisterFieldChange] = []
    unaffected: list[str] = []


class ReviewDecision(BaseModel):
    """One human decision recorded at the `human_gate` interrupt (8.6),
    mirroring the `reviews` table."""

    item_type: Literal["claim", "conflict", "finding", "register_change"]
    item_id: uuid.UUID
    decision: Literal["approve", "reject"]
    note: str | None = None
    reviewer: str


class AgentState(BaseModel):
    run_id: uuid.UUID
    corpus_id: uuid.UUID
    kind: Literal["initial", "update"]
    trigger_doc_id: uuid.UUID | None
    # populated as the graph runs
    documents: list[DocumentRef] = []
    classifications: dict[uuid.UUID, str] = {}
    claims: list[ClaimDraft] = []
    conflicts: list[ConflictDraft] = []
    findings: list[FindingDraft] = []
    register_diff: RegisterDiff | None = None
    review_decisions: list[ReviewDecision] = []
    error: str | None = None
