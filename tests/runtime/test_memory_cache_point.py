"""A sub-agent loaded with memory marks its end as cacheable for Bedrock.

The cache point goes on the copy each model call is sent, after the last loaded
message, so every model turn of the call can read the memory from the prompt
cache. It never lands in the agent's messages or in the history written back.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from strands import Agent, tool
from strands.models import CacheConfig

from kaboo_workflows._context import HistoryExchange, set_history_exchange
from kaboo_workflows.hooks import ContextHook, HistoryHook
from tests.fakes import LocalBedrockModel, ScriptedModel

MEMORY: list[Any] = [
    {"role": "user", "content": [{"text": "what was 2024 revenue?"}]},
    {"role": "assistant", "content": [{"text": "It was $42M."}]},
    {"role": "user", "content": [{"text": "and 2023?"}]},
    {"role": "assistant", "content": [{"text": "It was $40M."}]},
]


@pytest.fixture(autouse=True)
def _isolate_exchange() -> Iterator[None]:
    set_history_exchange(None)
    yield
    set_history_exchange(None)


def _cache_points(messages: list[Any]) -> list[int]:
    return [i for i, m in enumerate(messages) for b in m["content"] if "cachePoint" in b]


@tool
def lookup(key: str) -> str:
    """Look a value up."""
    return "value"


def _remembering_agent(model: Any, memory: list[Any]) -> tuple[Agent, HistoryExchange]:
    exchange = HistoryExchange(inbound={"k": memory})
    set_history_exchange(exchange)
    agent = Agent(
        model=model,
        tools=[lookup],
        hooks=[ContextHook(), HistoryHook("k", enabled=True)],
        callback_handler=None,
    )
    return agent, exchange


async def test_every_model_turn_of_the_call_has_a_cache_point_after_the_loaded_memory() -> None:
    model = LocalBedrockModel(tool="lookup")
    agent, _ = _remembering_agent(model, MEMORY)

    await agent.invoke_async("and 2022?")

    assert len(model.calls) == 2
    for sent in model.calls:
        assert _cache_points(sent) == [len(MEMORY) - 1]
        assert sent[len(MEMORY) - 1]["content"][-1] == {"cachePoint": {"type": "default"}}


async def test_a_cache_point_is_never_kept_or_written_back() -> None:
    stored = [
        *MEMORY[:-1],
        {**MEMORY[-1], "content": [*MEMORY[-1]["content"], {"cachePoint": {"type": "default"}}]},
    ]
    model = LocalBedrockModel()
    agent, exchange = _remembering_agent(model, stored)

    await agent.invoke_async("and 2022?")

    assert _cache_points(model.calls[-1]) == [len(MEMORY) - 1]
    assert _cache_points(agent.messages) == []
    assert _cache_points(exchange.written_back()["k"]) == []


@pytest.mark.parametrize(
    "model",
    [
        pytest.param(ScriptedModel(final_text="Done."), id="not-bedrock"),
        pytest.param(
            LocalBedrockModel(cache_config=CacheConfig(strategy="auto")), id="bedrock-caches-itself"
        ),
    ],
)
async def test_no_cache_point_is_added_where_it_would_not_reach_bedrock(model: Any) -> None:
    agent, _ = _remembering_agent(model, MEMORY)

    await agent.invoke_async("and 2022?")

    assert _cache_points(model.calls[-1]) == []


async def test_no_cache_point_is_added_without_loaded_memory() -> None:
    model = LocalBedrockModel()
    agent, _ = _remembering_agent(model, [])

    await agent.invoke_async("what was 2024 revenue?")

    assert _cache_points(model.calls[-1]) == []
