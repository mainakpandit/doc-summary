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

_SMELL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"ignore (all|any|the)?\s*(previous|prior|above)\s*instructions",
        r"disregard (all|any|the)?\s*(previous|prior|above)\s*(instructions|prompt)",
        r"new instructions\s*:",
        r"system prompt",
        r"you are now\b",
        r"act as (a|an)\b",
        r"reveal your (instructions|prompt|system prompt)",
        r"do not (tell|inform|notify) (the )?(user|reviewer)",
    ]
]


def wrap_sources(sources: list[tuple[uuid.UUID, str]]) -> str:
    """Wrap each `(chunk_id, text)` pair in an `<untrusted_source id="...">`
    block, joined with blank lines. The id lets a system prompt instruct
    the model to cite `chunk_id` back verbatim when it quotes a passage."""
    return "\n\n".join(
        f'<untrusted_source id="{chunk_id}">\n{text}\n</untrusted_source>'
        for chunk_id, text in sources
    )


def scan_response(text: str) -> list[str]:
    """Return the injection-smell patterns matched in `text` (an LLM
    response), empty if none. Heuristic: a miss doesn't mean the response
    is safe, and a hit doesn't mean the run was compromised -- only that a
    human should look, via the Finding the caller writes for it."""
    return [pattern.pattern for pattern in _SMELL_PATTERNS if pattern.search(text)]
