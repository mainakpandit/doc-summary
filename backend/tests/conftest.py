"""Shared pytest fixtures.

`fake_llm` and `fake_embedder` monkeypatch the swappable provider
factories in services/llm.py and services/embeddings.py so tests get
deterministic, offline doubles (backend/tests/fakes.py) instead of the
real Anthropic/embedding providers. Neither fixture requires
ANTHROPIC_API_KEY (CLAUDE.md behavior 7).
"""

import pytest

from backend.app.services import embeddings as embeddings_service
from backend.app.services import llm as llm_service
from backend.tests.fakes import FakeEmbedder, FakeLLM


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm_service, "_provider_factory", lambda: fake)
    return fake


@pytest.fixture
def fake_embedder(monkeypatch) -> FakeEmbedder:
    fake = FakeEmbedder()
    monkeypatch.setattr(embeddings_service, "_provider_factory", lambda: fake)
    return fake
