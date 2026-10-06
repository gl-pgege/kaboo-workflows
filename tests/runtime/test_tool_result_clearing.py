"""Old tool results are cleared from what a model is sent once its input is large.

A cleared result keeps its call, reads as a placeholder naming the tool and the
ids it carried, and stays cleared the same way on every later call. The agent's
own messages keep every result.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from strands import Agent, tool

from kaboo_workflows.config.resolvers.agents import build_agent_from_def
from kaboo_workflows.hooks import ContextHook
from kaboo_workflows.hooks._transcript import clear_tool_results
from kaboo_workflows.tools import node_as_async_tool
from tests.factories import agent_def
from tests.fakes import ScriptedModel

BIG = "x" * 2000
TOOLS = {"athena___run_query", "github___search", "analyze_sql", "query"}


def _transcript(*steps: tuple[str, str]) -> list[Any]:
    """A brief, one call per ``(tool, result text)`` step, then a report."""
    messages: list[Any] = [{"role": "user", "content": [{"text": "brief"}]}]
    for n, (name, text) in enumerate(steps):
        messages.append(
            {
                "role": "assistant",
                "content": [{"toolUse": {"toolUseId": f"c{n}", "name": name, "input": {"n": n}}}],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": f"c{n}",
                            "status": "success",
                            "content": [{"text": text}],
                        }
                    }
                ],
            }
        )
    messages.append({"role": "assistant", "content": [{"text": "report"}]})
    return messages


def _results(messages: list[Any]) -> list[str]:
    return [
        block["toolResult"]["content"][0]["text"]
        for message in messages
        for block in message["content"]
        if "toolResult" in block
    ]


def _uses(messages: list[Any]) -> list[Any]:
    return [b["toolUse"] for m in messages for b in m["content"] if "toolUse" in b]


def _turns(messages: list[Any]) -> list[dict[str, Any]]:
    """The conversation alone, without the bookkeeping keys strands adds to messages."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _clear(messages: list[Any], **overrides: Any) -> list[Any]:
    options: dict[str, Any] = {
        "keep_last": 3,
        "min_chars": 1500,
        "kept": set(),
        "tools": TOOLS,
        **overrides,
    }
    return clear_tool_results(messages, **options)


def _placeholder(name: str, *ids: str) -> str:
    return f"Result cleared ({', '.join([name, *ids])}): call it again to see it."


def test_results_outside_the_last_keep_last_turns_are_cleared() -> None:
    messages = _transcript(*[("athena___run_query", BIG)] * 5)

    cleared = _clear(messages, keep_last=2)

    assert _results(cleared) == [_placeholder("athena___run_query")] * 3 + [BIG] * 2


def _batches(*sizes: int) -> list[Any]:
    """A brief, then one model response per size calling that many tools at once."""
    messages: list[Any] = [{"role": "user", "content": [{"text": "brief"}]}]
    for turn, size in enumerate(sizes):
        ids = [f"t{turn}c{n}" for n in range(size)]
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": i, "name": "athena___run_query", "input": {}}}
                    for i in ids
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": i,
                            "status": "success",
                            "content": [{"text": BIG}],
                        }
                    }
                    for i in ids
                ],
            }
        )
    return messages


def test_a_batch_of_parallel_results_stays_or_goes_as_one_turn() -> None:
    messages = _batches(2, 3, 9)

    cleared = _clear(messages, keep_last=1)

    # The nine results the model is about to read arrive whole.
    assert _results(cleared) == [_placeholder("athena___run_query")] * 5 + [BIG] * 9


def test_keep_last_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="keep_last"):
        agent_def(model="m", context={"clear_tool_results": {"trigger": 100, "keep_last": 0}})


def test_results_shorter_than_min_chars_are_never_cleared() -> None:
    messages = _transcript(("athena___run_query", "3 rows"), ("athena___run_query", BIG))

    cleared = _clear(messages, keep_last=0)

    assert _results(cleared) == ["3 rows", _placeholder("athena___run_query")]


def test_keep_tools_and_delegates_are_never_cleared() -> None:
    messages = _transcript(
        ("github___search", BIG),
        ("analyze_sql", BIG),
        ("researcher", BIG),
        ("athena___run_query", BIG),
    )

    cleared = _clear(messages, keep_last=0, kept={"github___search", "analyze_sql"})

    # researcher is not one of the agent's tools and has no "___": a delegate.
    assert _results(cleared) == [BIG, BIG, BIG, _placeholder("athena___run_query")]


def test_a_cleared_result_keeps_its_call_and_names_the_ids_it_held() -> None:
    result = {
        "queryExecutionId": "q-1",
        "rows": [{"customer_id": 7, "name": "A"}, {"customer_id": 9}],
        "valid": True,
        "padding": BIG,
    }
    messages = _transcript(("athena___run_query", json.dumps(result)))

    cleared = _clear(messages, keep_last=0)

    assert _results(cleared) == [
        _placeholder("athena___run_query", "queryExecutionId=q-1", "customer_id=7", "customer_id=9")
    ]
    assert _uses(cleared) == _uses(messages)


def test_a_placeholder_names_at_most_ten_ids() -> None:
    result = {"rows": [{"rowId": n} for n in range(25)], "padding": BIG}
    messages = _transcript(("athena___run_query", json.dumps(result)))

    cleared = _clear(messages, keep_last=0)

    assert _results(cleared) == [
        _placeholder("athena___run_query", *(f"rowId={n}" for n in range(10)))
    ]


def test_clearing_twice_changes_nothing_and_a_cleared_prefix_stays_identical() -> None:
    five = _transcript(*[("athena___run_query", BIG)] * 5)
    seven = _transcript(*[("athena___run_query", BIG)] * 7)

    first = _clear(five)
    later = _clear(seven)

    assert _clear(first) == first
    # Brief plus the two cleared calls of the shorter run, byte for byte.
    assert later[:5] == first[:5]


def test_the_messages_passed_in_are_not_changed() -> None:
    messages = _transcript(*[("athena___run_query", BIG)] * 4)
    before = copy.deepcopy(messages)

    _clear(messages, keep_last=0)

    assert messages == before


@tool
def query(sql: str) -> str:
    """Run a SQL query."""
    return "rows"


@pytest.mark.parametrize(("trigger", "cleared"), [(1_000_000, 0), (100, 1)])
async def test_the_hook_clears_only_past_its_trigger_and_the_agent_keeps_every_result(
    trigger: int, cleared: int
) -> None:
    history = _transcript(*[("query", BIG)] * 4)
    model = ScriptedModel(final_text="done")
    agent = Agent(
        model=model,
        tools=[query],
        hooks=[ContextHook(clear_trigger=trigger)],
        messages=copy.deepcopy(history),
        callback_handler=None,
    )

    await agent.invoke_async("next")

    sent = _results(model.calls[-1])
    assert sent.count(_placeholder("query")) == cleared
    assert _turns(agent.messages[: len(history)]) == history


async def test_the_hook_never_clears_a_delegates_result() -> None:
    researcher = Agent(
        model=ScriptedModel(final_text="findings"),
        name="researcher",
        agent_id="researcher",
        callback_handler=None,
    )
    history = _transcript(*[("researcher", BIG)] * 4)
    model = ScriptedModel(final_text="done")
    agent = Agent(
        model=model,
        tools=[node_as_async_tool(researcher, description="Researches")],
        hooks=[ContextHook(clear_trigger=1, keep_last=0)],
        messages=copy.deepcopy(history),
        callback_handler=None,
    )

    await agent.invoke_async("next")

    assert _results(model.calls[-1]) == [BIG] * 4


async def test_context_clear_tool_results_reaches_the_built_agent() -> None:
    model = ScriptedModel(final_text="done")
    definition = agent_def(
        model="m",
        tools=["tests.fakes.gated_tools:submit_report"],
        context={"clear_tool_results": {"trigger": 100, "keep_last": 1}},
    )
    agent = build_agent_from_def("analyst", definition, models={"m": model}, mcp_clients={})
    agent.messages = _transcript(*[("submit_report", BIG)] * 3)

    await agent.invoke_async("next")

    assert _results(model.calls[-1]) == [_placeholder("submit_report")] * 2 + [BIG]
