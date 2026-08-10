"""Test doubles for the LLM and embedding provider seams.

`FakeLLM` stands in for `services.llm.LLMProvider` and `FakeEmbedder` for
`services.embeddings.EmbedderProvider`. Neither talks to a real API, so
tests using them never need `ANTHROPIC_API_KEY` (CLAUDE.md behavior 7).
See `backend/tests/conftest.py` for the `fake_llm` / `fake_embedder`
fixtures that monkeypatch each service's `_provider_factory` with these.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.app.services.embeddings import EmbedderProvider
from backend.app.services.llm import LLMProvider, LLMResponse

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "llm"


class MissingFixtureError(Exception):
    """Raised by FakeLLM.complete when no fixture is recorded for a
    (stage, hash) key, so the developer knows exactly what to add and
    where."""


def cache_key(stage: str, system: str, messages: list[dict[str, Any]]) -> str:
    """The lookup key FakeLLM uses: stage plus a hash of the exact
    prompt content, so two calls with identical text but different
    stages (or vice versa) get distinct fixtures."""
    digest = hashlib.sha256(
        (system + json.dumps(messages, sort_keys=True)).encode("utf-8")
    ).hexdigest()
    return f"{stage}:{digest}"


def _load_fixtures(fixtures_dir: Path) -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    for path in sorted(fixtures_dir.glob("*.json")):
        responses.update(json.loads(path.read_text()))
    return responses


@dataclass
class LLMCall:
    stage: str
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    model: str


class FakeLLM(LLMProvider):
    """Drop-in LLMProvider that answers from JSON fixtures under
    backend/tests/fixtures/llm/*.json instead of calling Anthropic."""

    def __init__(self, fixtures_dir: Path | None = None) -> None:
        self._fixtures_dir = fixtures_dir or FIXTURES_DIR
        self._responses = _load_fixtures(self._fixtures_dir)
        self.calls: list[LLMCall] = []

    async def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        stage: str,
    ) -> LLMResponse:
        self.calls.append(
            LLMCall(stage=stage, system=system, messages=messages, tools=tools, model=model)
        )

        key = cache_key(stage, system, messages)
        fixture = self._responses.get(key)
        if fixture is None:
            raise MissingFixtureError(
                f"FakeLLM: no fixture recorded for key {key!r} (stage={stage!r}). "
                f"Add an entry to a JSON file under {self._fixtures_dir}, e.g.:\n"
                f'  "{key}": {{"text": "...", "input_tokens": 0, "output_tokens": 0, '
                f'"stop_reason": "end_turn"}}\n'
                f"Call was: system={system!r} messages={messages!r}"
            )

        return LLMResponse(
            text=fixture["text"],
            input_tokens=fixture.get("input_tokens", 0),
            output_tokens=fixture.get("output_tokens", 0),
            stop_reason=fixture.get("stop_reason"),
            raw=fixture,
        )


class FakeEmbedder(EmbedderProvider):
    """Drop-in EmbedderProvider returning deterministic pseudo-random unit
    vectors derived from sha256(text), so the same text always embeds to
    the same vector without calling a real embedding API."""

    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        rng = random.Random(int.from_bytes(digest, "big"))
        raw = [rng.uniform(-1.0, 1.0) for _ in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in raw)) or 1.0
        return [v / norm for v in raw]
