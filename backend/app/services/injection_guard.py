"""Injection defense around the one place source-chunk text crosses into an
LLM prompt (CLAUDE.md behavior 8: "All source text is wrapped in
`<untrusted_source id=...>` before reaching the LLM. `scan_response` flags
injection smells; hits produce `possible_prompt_injection` findings, never
silent side effects.").

`wrap_sources` is the only sanctioned way chunk text reaches `call_claude`
-- nodes must not concatenate chunk text into a prompt by hand (see
`agent/nodes/extract.py`, the first node to send chunk text). `scan_response`
runs on the text that comes back and flags responses that look like they
followed instructions embedded in a source rather than the system prompt.
Both are heuristics: `wrap_sources` reduces the model's odds of treating
source content as instructions, it doesn't guarantee immunity, and
`scan_response` is a smell test, not a proof of compromise. Turning a hit
into a `possible_prompt_injection` Finding row is the caller's job -- this
module only wraps and detects, it never writes to the database.
"""

from __future__ import annotations

import re
import uuid
from typing import NamedTuple

# Matches a literal `<untrusted_source ...>` or `</untrusted_source>` tag
# appearing *inside* chunk text -- a chunk author (i.e. whoever wrote the
# source document) could otherwise forge a fake closing tag to make content
# placed after it look like it sits outside the wrapper. `wrap_sources`
# neutralizes any such tag-shaped text before wrapping, so the only real
# `<untrusted_source>`/`</untrusted_source>` tags in its output are the ones
# it adds itself.
_FORGED_TAG_RE = re.compile(r"</?untrusted_source\b[^>]*>", re.IGNORECASE)


def _escape_forged_tags(text: str) -> str:
    return _FORGED_TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text)


def wrap_sources(sources: list[tuple[uuid.UUID, str]]) -> str:
    """Wrap each `(chunk_id, text)` pair in an `<untrusted_source id="...">`
    block, joined with blank lines. The id lets a system prompt instruct
    the model to cite `chunk_id` back verbatim when it quotes a passage.
    Nothing is ever emitted outside a block: the return value is exactly
    the wrapped blocks joined together, and any tag-shaped text inside a
    chunk is escaped first so it can't forge a fake block boundary."""
    return "\n\n".join(
        f'<untrusted_source id="{chunk_id}">\n{_escape_forged_tags(text)}\n</untrusted_source>'
        for chunk_id, text in sources
    )


class Smell(NamedTuple):
    """One injection-smell hit. `category` names which defense it tripped
    (see `_SMELL_RULES` below); `excerpt` is the literal matched text, kept
    short enough to drop straight into a Finding.message."""

    category: str
    excerpt: str


# Each category maps to one or more patterns; `scan_response` reports every
# pattern that matches, not just the first hit overall. Five categories are
# the ones CLAUDE.md behavior 8 / the injection-guard task explicitly name;
# the rest are pre-existing extra coverage (role/system-prompt probes) kept
# because they cost nothing and catch real smells the five don't.
_SMELL_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_previous_instructions",
        re.compile(
            r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above)\s+"
            r"(?:instructions|rules|prompts?|directives|context)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_instructions",
        re.compile(
            r"\bdisregard\s+(?:(?:all|any|the)\s+)?(?:(?:previous|prior|above)\s+)?"
            r"(?:instructions|rules|prompts?|directives)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "fetch_url",
        re.compile(
            r"\b(?:fetch|download|retrieve|curl|wget|visit|open|navigate to|go to)\b"
            r"[^.\n]{0,60}https?://\S+",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltrate_data",
        re.compile(
            r"\b(?:email|send|upload|exfiltrate|forward|transmit|post)\b[^.\n]{0,80}"
            r"(?:@[\w.-]+\.\w+|https?://\S+)",
            re.IGNORECASE,
        ),
    ),
    (
        "change_tool_behavior",
        re.compile(
            r"\b(?:mark|treat|flag|set)\b[^.\n]{0,40}\b(?:every|all)\b[^.\n]{0,40}"
            r"\b(?:approved|passed|resolved|clean|valid)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "change_tool_behavior",
        re.compile(
            r"\b(?:auto[- ]?approve|skip (?:the )?(?:human )?review|"
            r"bypass (?:the )?(?:human )?(?:gate|review))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "system_prompt_probe",
        re.compile(r"system prompt|reveal your (?:instructions|prompt)", re.IGNORECASE),
    ),
    ("role_override", re.compile(r"you are now\b|act as (?:a|an)\b", re.IGNORECASE)),
    (
        "silence_reviewer",
        re.compile(r"do not (?:tell|inform|notify) (?:the )?(?:user|reviewer)", re.IGNORECASE),
    ),
]


def scan_response(text: str) -> list[Smell]:
    """Return every injection-smell pattern matched in `text` (an LLM
    response), empty if none. Heuristic: a miss doesn't mean the response
    is safe, and a hit doesn't mean the run was compromised -- only that a
    human should look, via the Finding the caller writes for it."""
    smells: list[Smell] = []
    for category, pattern in _SMELL_RULES:
        match = pattern.search(text)
        if match is not None:
            smells.append(Smell(category=category, excerpt=match.group(0)))
    return smells
