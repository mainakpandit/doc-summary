"""Single choke point for every Claude API call (see CLAUDE.md rule:
"Every LLM call: call_claude(session, run_id, stage, ...). No direct
`anthropic` client usage in nodes or routes.").

`call_claude` enforces the per-run cost budget before spending any money,
times the call, and writes a `cost_events` row after every completion so
cost is attributed per (run_id, stage) and survives a killed-and-restarted
worker (behavior 2: killed stages are not re-billed, because nothing here
is billed until the row is committed).

The provider is selected through a module-level factory rather than
constructed inline, so tests can monkeypatch `_provider_factory` to swap
in `FakeLLM` (backend/tests/fakes.py) without touching call sites or
requiring an ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import structlog
from anthropic import AsyncAnthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import get_settings
from backend.app.models.cost import CostEvent

logger = structlog.get_logger(__name__)

# USD per 1M tokens. Source: https://platform.claude.com/docs/en/pricing
# (Claude Sonnet 5 standard rate, not the introductory rate). Checked
# 2026-08-10 — re-check this table whenever DEFAULT_MODEL changes or
# pricing is refreshed.
PRICES: dict[str, dict[str, Decimal]] = {
    "claude-sonnet-5": {
        "input": Decimal("3.00"),
        "output": Decimal("15.00"),
    },
}

DEFAULT_MODEL = "claude-sonnet-5"

# Hard ceiling passed to every completion. Also used, together with an
# estimate of input size, as the worst-case cost for the pre-call budget
# check below — we don't know real output size until the call returns.
MAX_TOKENS = 8192

_MILLION = Decimal(1_000_000)


class BudgetExceeded(Exception):
    """Raised when a call's estimated cost would push a run's total spend
    past settings.COST_BUDGET_USD_PER_RUN. Callers (agent nodes) should
    let this fail the run rather than catching and retrying — silently
    continuing would defeat the budget."""


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    raw: Any


class LLMProvider(Protocol):
    async def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        stage: str,
    ) -> LLMResponse: ...


class AnthropicProvider:
    """Talks to the real Anthropic API via the async SDK client."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

    async def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        stage: str,
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        response = await self._client.messages.create(**kwargs)

        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            raw=response,
        )


def _default_provider() -> LLMProvider:
    return AnthropicProvider()


# Swappable factory — backend/tests/fakes.py monkeypatches this attribute
# (not call_claude itself) so FakeLLM stands in for every call site.
_provider_factory: Callable[[], LLMProvider] = _default_provider


def _prices_for(model: str) -> dict[str, Decimal]:
    return PRICES.get(model, PRICES[DEFAULT_MODEL])


def _usd_cost(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    prices = _prices_for(model)
    return (
        Decimal(input_tokens) / _MILLION * prices["input"]
        + Decimal(output_tokens) / _MILLION * prices["output"]
    )


def _estimate_worst_case_cost(system: str, messages: list[dict[str, Any]], model: str) -> Decimal:
    """Conservative pre-call estimate: ~4 chars/token for input, MAX_TOKENS
    as the output ceiling (real output tokens aren't known until the call
    returns). Deliberately overestimates so the budget check fails closed."""
    input_chars = len(system) + len(json.dumps(messages))
    estimated_input_tokens = max(input_chars // 4, 1)
    return _usd_cost(model, estimated_input_tokens, MAX_TOKENS)


async def call_claude(
    session: AsyncSession,
    run_id: uuid.UUID,
    stage: str,
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> LLMResponse:
    settings = get_settings()
    model = model or DEFAULT_MODEL

    spent = await session.scalar(
        select(func.coalesce(func.sum(CostEvent.usd_cost), 0)).where(CostEvent.run_id == run_id)
    )
    estimated = _estimate_worst_case_cost(system, messages, model)
    if Decimal(spent) + estimated > Decimal(str(settings.COST_BUDGET_USD_PER_RUN)):
        raise BudgetExceeded(
            f"run {run_id} stage {stage!r}: spent ${spent} + estimated ${estimated} "
            f"would exceed budget ${settings.COST_BUDGET_USD_PER_RUN}"
        )

    provider = _provider_factory()

    start = time.monotonic()
    result = await provider.complete(system, messages, tools, model, stage)
    latency_ms = int((time.monotonic() - start) * 1000)

    usd_cost = _usd_cost(model, result.input_tokens, result.output_tokens)

    session.add(
        CostEvent(
            run_id=run_id,
            stage=stage,
            model=model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=latency_ms,
            usd_cost=usd_cost,
        )
    )
    await session.commit()

    logger.info(
        "llm_call",
        run_id=str(run_id),
        stage=stage,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        usd_cost=str(usd_cost),
        latency_ms=latency_ms,
    )

    return result
