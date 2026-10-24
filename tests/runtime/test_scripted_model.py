"""The scripted model reads its step from the conversation, so fresh instances agree."""

from __future__ import annotations

import json
from typing import Any

from kaboo_workflows.testing import ScriptedModel

LOOKUP = {
    "name": "lookup",
    "inputSchema": {"json": {"type": "object", "properties": {"query": {"type": "string"}}}},
}


def _human(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _assistant_tool(tool_use_id: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"toolUse": {"toolUseId": tool_use_id, "name": "lookup", "input": {}}}],
    }


def _result(tool_use_id: str) -> dict[str, Any]:
    return {
        "role": "user",
        "content": [{"toolResult": {"toolUseId": tool_use_id, "content": [{"text": "ok"}]}}],
    }


async def _collect(model: ScriptedModel, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event async for event in model.stream(messages, tool_specs=[LOOKUP])]


def _text(events: list[dict[str, Any]]) -> str:
    return "".join(
        e["contentBlockDelta"]["delta"].get("text", "") for e in events if "contentBlockDelta" in e
    )


def _tool_input(events: list[dict[str, Any]]) -> str:
    return "".join(
        e["contentBlockDelta"]["delta"]["toolUse"]["input"]
        for e in events
        if "contentBlockDelta" in e and "toolUse" in e["contentBlockDelta"]["delta"]
    )


async def test_a_fresh_instance_continues_from_the_conversation() -> None:
    script = [{"tool": "lookup", "input": "order 42"}, {"text": "Shipped."}]
    first = await _collect(ScriptedModel(script=script), [_human("where is order 42?")])
    assert json.loads(_tool_input(first)) == {"query": "order 42"}

    resumed = await _collect(
        ScriptedModel(script=script),
        [_human("where is order 42?"), _assistant_tool("call-1"), _result("call-1")],
    )
    assert _text(resumed) == "Shipped."


async def test_turns_give_each_human_turn_its_own_script_and_the_last_repeats() -> None:
    model = ScriptedModel(turns=[[{"text": "first"}], [{"text": "second"}]])
    history = [_human("one"), {"role": "assistant", "content": [{"text": "first"}]}]
    assert _text(await _collect(model, [_human("one")])) == "first"
    assert _text(await _collect(model, [*history, _human("two")])) == "second"
    later = [
        *history,
        _human("two"),
        {"role": "assistant", "content": [{"text": "second"}]},
        _human("three"),
    ]
    assert _text(await _collect(model, later)) == "second"


async def test_stream_chunk_splits_text_and_tool_arguments_into_deltas() -> None:
    model = ScriptedModel(
        script=[{"tool": "lookup", "input": "a fairly long query string"}], stream_chunk=5
    )
    events = await _collect(model, [_human("go")])
    deltas = [e for e in events if "contentBlockDelta" in e]
    assert len(deltas) > 3
    assert json.loads(_tool_input(events)) == {"query": "a fairly long query string"}

    text_events = await _collect(
        ScriptedModel(script=[{"text": "hello world"}], stream_chunk=4), [_human("hi")]
    )
    assert _text(text_events) == "hello world"
    assert len([e for e in text_events if "contentBlockDelta" in e]) == 3
