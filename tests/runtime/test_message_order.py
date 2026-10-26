from __future__ import annotations

from typing import Any

from ag_ui.core import (
    EventType,
    ReasoningMessageStartEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

from kaboo_workflows.adapters._order import AppendOnly
from kaboo_workflows.adapters._subagents import SubagentStream
from kaboo_workflows.types import EventType as Published
from kaboo_workflows.types import StreamEvent
from tests.fakes.client import drawn


def text(message_id: str, words: str, run: str | None = None) -> list[Any]:
    return [
        TextMessageStartEvent(
            type=EventType.TEXT_MESSAGE_START,
            message_id=message_id,
            role="assistant",
            subagent_run_id=run,
        ),
        TextMessageContentEvent(
            type=EventType.TEXT_MESSAGE_CONTENT,
            message_id=message_id,
            delta=words,
            subagent_run_id=run,
        ),
        TextMessageEndEvent(
            type=EventType.TEXT_MESSAGE_END, message_id=message_id, subagent_run_id=run
        ),
    ]


def reasoning(message_id: str) -> list[Any]:
    return [
        ReasoningMessageStartEvent(
            type=EventType.REASONING_MESSAGE_START, message_id=message_id, role="reasoning"
        )
    ]


def call(call_id: str, name: str, parent: str | None) -> list[Any]:
    return [
        ToolCallStartEvent(
            type=EventType.TOOL_CALL_START,
            tool_call_id=call_id,
            tool_call_name=name,
            parent_message_id=parent,
        )
    ]


def result(message_id: str, call_id: str) -> list[Any]:
    return [
        ToolCallResultEvent(
            type=EventType.TOOL_CALL_RESULT,
            message_id=message_id,
            tool_call_id=call_id,
            content="ok",
            role="tool",
        )
    ]


def placed(events: list[Any]) -> list[Any]:
    order = AppendOnly()
    return [order.place(event) for event in events]


def parents(events: list[Any]) -> list[str | None]:
    return [e.parent_message_id for e in events if e.type == EventType.TOOL_CALL_START]


def test_a_call_made_after_reasoning_is_drawn_after_it() -> None:
    events = [*text("a", "Let me check."), *reasoning("r"), *call("c1", "lookup", parent="a")]

    assert drawn(events) == ["Let me check.", "call lookup", "reasoning"]
    assert drawn(placed(events)) == ["Let me check.", "reasoning", "call lookup"]


def test_calls_that_follow_their_text_stay_with_it() -> None:
    events = [
        *reasoning("r"),
        *text("a", "Two lookups."),
        *call("c1", "lookup", parent="a"),
        *call("c2", "fetch", parent="a"),
    ]
    out = placed(events)

    assert parents(out) == ["a", "a"]
    assert drawn(out) == ["reasoning", "Two lookups.", "call lookup", "call fetch"]


def test_parallel_calls_after_reasoning_share_one_new_message() -> None:
    events = [
        *text("a", "Two lookups."),
        *reasoning("r"),
        *call("c1", "lookup", parent="a"),
        *call("c2", "fetch", parent="a"),
    ]
    out = placed(events)

    [parent] = set(parents(out))
    assert parent != "a"
    assert drawn(out) == ["Two lookups.", "reasoning", "call lookup", "call fetch"]


def test_text_after_calls_gets_a_message_of_its_own() -> None:
    events = [*call("c1", "lookup", parent="m"), *text("m", "Looked it up.")]

    assert drawn(events) == ["Looked it up.", "call lookup"]
    assert drawn(placed(events)) == ["call lookup", "Looked it up."]


def test_a_call_naming_a_message_from_before_a_result_moves_after_it() -> None:
    events = [
        *text("a", "First."),
        *call("c1", "lookup", parent="a"),
        *result("t1", "c1"),
        *text("b", "Second."),
        *call("c2", "fetch", parent="a"),
    ]

    assert drawn(placed(events)) == ["First.", "call lookup", "tool", "Second.", "call fetch"]


def test_a_sub_agents_messages_do_not_move_the_entry_agents_calls() -> None:
    events = [
        *text("a", "Asking the filer."),
        *text("s", "Filing.", run="filer-1"),
        *call("c1", "filer", parent="a"),
    ]
    out = placed(events)

    assert parents(out) == ["a"]
    assert drawn(out) == ["Asking the filer.", "call filer"]
    assert drawn(out, run="filer-1") == ["Filing."]


def published(kind: str, **data: Any) -> StreamEvent:
    return StreamEvent(type=kind, agent_name="filer", data={"stream_group": "g1", **data})


def test_a_sub_agent_that_reasons_before_its_call_gives_the_call_a_message_of_its_own() -> None:
    stream = SubagentStream()
    events: list[Any] = [
        *stream.translate(published(Published.STREAM_GROUP_START)),
        *stream.translate(published(Published.TOKEN, text="Filing.")),
        *stream.translate(published(Published.REASONING, text="The title is Q3.")),
        *stream.translate(
            published(Published.TOOL_INPUT, tool_use_id="t1", tool_name="submit_report", input="{}")
        ),
    ]
    text_id = next(e.message_id for e in events if e.type == EventType.TEXT_MESSAGE_START)

    assert parents(events) != [text_id]
    assert drawn(events, run="g1") == ["Filing.", "reasoning", "call submit_report"]
