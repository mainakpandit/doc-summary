"""Provider seam for chunk/query embedding calls.

Mirrors services/llm.py's swappable-factory pattern: `_provider_factory` is
a module-level attribute so tests can monkeypatch it to `FakeEmbedder`
(backend/tests/fakes.py, via the `fake_embedder` fixture in
backend/tests/conftest.py) without touching call sites or requiring a real
embedding API key.

The real Voyage-backed provider and `embed_chunks` (batching, retries,
writing `chunks.embedding`, and `cost_events` rows for the "embed" stage)
land in task_breakdown.md Step 14. This module currently defines only the
seam FakeEmbedder plugs into ahead of that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol


class EmbedderProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _default_provider() -> EmbedderProvider:
    raise NotImplementedError(
        "No real embedding provider yet (see task_breakdown.md Step 14). "
        "Tests should use the fake_embedder fixture from backend/tests/conftest.py."
    )


# Swappable factory — backend/tests/conftest.py's fake_embedder fixture
# monkeypatches this attribute (not any call site) so FakeEmbedder stands
# in for the real provider.
_provider_factory: Callable[[], EmbedderProvider] = _default_provider
