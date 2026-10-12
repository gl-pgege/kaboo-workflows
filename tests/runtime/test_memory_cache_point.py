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
from kaboo_workflows.hooks._transcript import settle_cache_points
from kaboo_workflows.testing import ScriptedModel
from tests.fakes import LocalBedrockModel

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


async def test_a_cache_point_never_opens_a_message() -> None:
    use = {"toolUseId": "m1", "name": "code-workbench___update_query", "input": {}}
    result = {"toolUseId": "m1", "status": "error", "content": [{"text": "Unknown tool"}]}
    memory: list[Any] = [
        {"role": "user", "content": [{"text": "update the query"}]},
        {"role": "assistant", "content": [{"toolUse": use}]},
        {"role": "user", "content": [{"toolResult": result}]},
    ]
    model = LocalBedrockModel()
    agent, _ = _remembering_agent(model, memory)

    await agent.invoke_async("try again")

    sent = model.calls[-1]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert _cache_points(sent) == [1]
    assert all("cachePoint" not in m["content"][0] for m in sent)


def test_a_cache_point_that_opens_a_message_moves_back_or_goes() -> None:
    point = {"cachePoint": {"type": "default"}}
    messages = [
        {"role": "user", "content": [point, {"text": "a"}]},
        {"role": "assistant", "content": [{"text": "b"}]},
        {"role": "user", "content": [point, {"text": "c"}]},
    ]

    assert settle_cache_points(messages) == [
        {"role": "user", "content": [{"text": "a"}]},
        {"role": "assistant", "content": [{"text": "b"}, point]},
        {"role": "user", "content": [{"text": "c"}]},
    ]


async def test_no_cache_point_is_added_without_loaded_memory() -> None:
    model = LocalBedrockModel()
    agent, _ = _remembering_agent(model, [])

    await agent.invoke_async("what was 2024 revenue?")

    assert _cache_points(model.calls[-1]) == []
