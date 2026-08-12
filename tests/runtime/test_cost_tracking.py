"""Per-invocation dollar-cost capture.

The model adapter deposits each LLM call's OpenRouter-reported ``cost`` into a
context-local stack of accumulators; the event publisher opens a box when an
invocation starts and drains it into the ``AGENT_COMPLETE`` usage payload.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kaboo_workflows.hooks import cost_tracking
from kaboo_workflows.hooks.cost_tracking import add_cost, pop_cost_box, push_cost_box
from kaboo_workflows.models_openai import KabooOpenAIModel


@pytest.fixture(autouse=True)
def _clean_stack() -> None:
    """Isolate from boxes left dangling by other tests in the same context."""
    cost_tracking._stack.set(())


def test_pop_without_push_returns_zero() -> None:
    assert pop_cost_box() == 0.0


def test_add_without_box_is_a_noop() -> None:
    add_cost(1.5)
    assert pop_cost_box() == 0.0


def test_push_add_pop_accumulates() -> None:
    push_cost_box()
    add_cost(0.01)
    add_cost(0.02)
    assert abs(pop_cost_box() - 0.03) < 1e-9


def test_nested_boxes_attribute_to_innermost_then_restore() -> None:
    push_cost_box()  # agent A
    add_cost(0.10)
    push_cost_box()  # nested agent B
    add_cost(0.02)
    assert abs(pop_cost_box() - 0.02) < 1e-9  # B completes
    add_cost(0.05)  # A's later model call
    assert abs(pop_cost_box() - 0.15) < 1e-9  # A completes


def test_parallel_tasks_have_isolated_stacks() -> None:
    async def invocation(amount: float) -> float:
        push_cost_box()
        add_cost(amount)
        await asyncio.sleep(0)
        add_cost(amount)
        return pop_cost_box()

    async def main() -> list[float]:
        return await asyncio.gather(invocation(0.01), invocation(0.10))

    totals = asyncio.run(main())
    assert abs(totals[0] - 0.02) < 1e-9
    assert abs(totals[1] - 0.20) < 1e-9


def _metadata_event(usage: Any) -> dict[str, Any]:
    return {"chunk_type": "metadata", "data": usage}


def test_model_captures_cost_from_metadata_chunk() -> None:
    model = KabooOpenAIModel(model_id="anthropic/claude-haiku-4.5", client_args={"api_key": "x"})
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=5,
        total_tokens=105,
        prompt_tokens_details=SimpleNamespace(cached_tokens=80),
        cost=0.0123,
    )

    push_cost_box()
    chunk = model.format_chunk(_metadata_event(usage))
    assert abs(pop_cost_box() - 0.0123) < 1e-9
    assert chunk["metadata"]["usage"]["cacheReadInputTokens"] == 80


def test_model_captures_cost_from_model_extra() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=1,
        total_tokens=11,
        prompt_tokens_details=None,
        model_extra={"cost": 0.005},
    )
    model = KabooOpenAIModel(model_id="anthropic/claude-haiku-4.5", client_args={"api_key": "x"})

    push_cost_box()
    model.format_chunk(_metadata_event(usage))
    assert abs(pop_cost_box() - 0.005) < 1e-9


def test_model_tolerates_missing_cost() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10, completion_tokens=1, total_tokens=11, prompt_tokens_details=None
    )
    model = KabooOpenAIModel(model_id="anthropic/claude-haiku-4.5", client_args={"api_key": "x"})

    push_cost_box()
    chunk = model.format_chunk(_metadata_event(usage))
    assert pop_cost_box() == 0.0
    assert chunk["metadata"]["usage"]["inputTokens"] == 10
