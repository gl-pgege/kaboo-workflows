"""A transcript split into one message per tool call reaches the model as whole turns.

The chat stores a response that called two tools at once as a text message and
one message per call, followed by the results. Replayed one message per turn,
the results would answer only the last call, which Bedrock rejects. Only the
copy sent to the model is joined; the agent keeps the messages it was given.
"""

from __future__ import annotations

import copy
from typing import Any

from strands import Agent, tool

from kaboo_workflows.hooks import ContextHook
from kaboo_workflows.testing import ScriptedModel
from tests.runtime.test_foreign_tool_calls import (
    _assert_bedrock_valid,
    _assistant,
    _result,
    _turns,
    _use,
    _user,
)


@tool
def query(sql: str) -> str:
    """Run a SQL query."""
    return "rows"


async def test_parallel_calls_stored_one_per_message_reach_the_model_as_one_turn() -> None:
    history: list[Any] = [
        _user("profile our customers"),
        _assistant({"text": "Checking the project first."}),
        _assistant(_use("q1", "query", {"sql": "select 1"})),
        _assistant(_use("q2", "query", {"sql": "select 2"})),
        {"role": "user", "content": [_result("q1", "one"), _result("q2", "two")]},
        _assistant({"text": "Done."}),
    ]
    model = ScriptedModel(final_text="Melbourne leads.")
    agent = Agent(
        model=model,
        tools=[query],
        hooks=[ContextHook()],
        messages=copy.deepcopy(history),
        callback_handler=None,
    )

    await agent.invoke_async("which city leads?")

    sent = model.calls[-1]
    _assert_bedrock_valid(sent)
    assert sent[1]["content"] == [
        {"text": "Checking the project first."},
        _use("q1", "query", {"sql": "select 1"}),
        _use("q2", "query", {"sql": "select 2"}),
    ]
    assert _turns(agent.messages[: len(history)]) == history


async def test_user_turns_left_without_a_reply_reach_the_model_as_one_turn() -> None:
    history: list[Any] = [
        _user("profile our customers"),
        _assistant({"text": "Here is the profile."}),
        _user("which city leads?"),
        _user("turn the city chart into bars"),
    ]
    model = ScriptedModel(final_text="Done.")
    agent = Agent(
        model=model,
        tools=[query],
        hooks=[ContextHook()],
        messages=copy.deepcopy(history),
        callback_handler=None,
    )

    await agent.invoke_async("show me how it changed")

    sent = model.calls[-1]
    _assert_bedrock_valid(sent)
    assert [block["text"] for block in sent[-1]["content"]] == [
        "which city leads?",
        "turn the city chart into bars",
        "show me how it changed",
    ]
