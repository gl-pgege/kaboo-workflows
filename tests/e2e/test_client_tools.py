"""Browser tools: any agent may call the ones its allow-list names, and waits for the answer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from ag_ui.core import AssistantMessage, Context, FunctionCall, Tool, ToolCall, ToolMessage
from ag_ui.core import EventType as AGUIEventType
from ag_ui_a2ui_toolkit import A2UI_SCHEMA_CONTEXT_DESCRIPTION

from kaboo_workflows.testing import Pipeline, ScriptedModel, TurnResult

CONFIGS = Path(__file__).parent / "configs"
ASK_USER = Tool(
    name="ask_user",
    description="Ask the person multiple-choice questions.",
    parameters={"type": "object", "properties": {"questions": {"type": "array"}}},
)


def _pending(r: TurnResult) -> Any:
    outcome = r.run_outcome()
    assert getattr(outcome, "type", None) == "success", outcome
    [call_id] = outcome.pending_tool_call_ids
    return next(e for e in r.of_type(AGUIEventType.TOOL_CALL_START) if e.tool_call_id == call_id)


def _answer(r: TurnResult, call_id: str, content: str) -> list[Any]:
    return [
        *r.messages(),
        ToolMessage(id=f"answer-{call_id}", role="tool", tool_call_id=call_id, content=content),
    ]


async def test_the_entry_agent_waits_for_the_browser_and_reads_its_answer() -> None:
    pipe = Pipeline(str(CONFIGS / "client_tools_plain.yaml"))
    r1 = await pipe.turn("go", thread_id="b1", run_id="r1", tools=[ASK_USER])
    call = _pending(r1)
    assert call.tool_call_name == "ask_user" and not call.subagent_run_id
    assert r1.text() == ""

    r2 = await pipe.turn(
        thread_id="b1",
        run_id="r2",
        tools=[ASK_USER],
        messages=_answer(r1, call.tool_call_id, '{"answers": ["Europe"]}'),
    )
    assert r2.finished() and not r2.errored()
    assert "Looking at Europe." in r2.text()


SHOW_CANVAS = Tool(
    name="show_canvas",
    description="Open a canvas beside the chat.",
    parameters={
        "type": "object",
        "properties": {"canvas": {"type": "string"}, "ref": {"type": "string"}},
    },
)


async def test_a_browser_action_goes_on_at_once_and_the_run_ends_with_nothing_pending() -> None:
    pipe = Pipeline(str(CONFIGS / "client_tools_action.yaml"))
    r = await pipe.turn("show me", thread_id="b-action", run_id="r1", tools=[ASK_USER, SHOW_CANVAS])

    assert r.finished() and not r.errored()
    assert not r.run_outcome().pending_tool_call_ids
    [call] = r.of_type(AGUIEventType.TOOL_CALL_START)
    assert call.tool_call_name == "show_canvas"
    [result] = r.of_type(AGUIEventType.TOOL_CALL_RESULT)
    assert result.tool_call_id == call.tool_call_id
    assert "revenue rose 12%" in r.text()


async def test_a_delegated_agent_asks_the_browser_and_its_answer_comes_back_to_it() -> None:
    pipe = Pipeline(str(CONFIGS / "client_tools_delegate.yaml"))
    r1 = await pipe.turn("go", thread_id="b2", run_id="r1", tools=[ASK_USER])
    call = _pending(r1)
    assert call.tool_call_name == "ask_user"
    assert call.subagent_run_id == next(iter(r1.groups))
    assert r1.groups[call.subagent_run_id]["status"] == "suspended"

    r2 = await pipe.turn(
        thread_id="b2",
        run_id="r2",
        tools=[ASK_USER],
        messages=_answer(r1, call.tool_call_id, '{"answers": ["Europe"]}'),
    )
    assert r2.finished() and not r2.errored()
    assert "Coordinator done." in r2.text()
    delegate_result = next(
        e.content for e in r2.of_type(AGUIEventType.TOOL_CALL_RESULT) if not e.subagent_run_id
    )
    assert "Worker used the answer." in delegate_result
    assert "asked the person" in delegate_result and "Europe" in delegate_result


async def test_the_coordinator_reads_what_the_delegate_asked_in_plain_words() -> None:
    pipe = Pipeline(str(CONFIGS / "client_tools_delegate.yaml"))
    r1 = await pipe.turn("go", thread_id="b3", run_id="r1", tools=[ASK_USER])
    call = _pending(r1)

    r2 = await pipe.turn(
        thread_id="b3",
        run_id="r2",
        tools=[ASK_USER],
        messages=_answer(r1, call.tool_call_id, '{"Which region?": "Europe"}'),
    )
    delegate_result = json.loads(
        next(e.content for e in r2.of_type(AGUIEventType.TOOL_CALL_RESULT) if not e.subagent_run_id)
    )
    assert 'Asked "Which region?": Europe' in delegate_result
    assert "{" not in delegate_result


@pytest.mark.parametrize("config", ["client_tools_delegate.yaml", "client_tools_nested.yaml"])
async def test_a_delegated_question_is_answered_in_another_process(config: str) -> None:
    r1 = await Pipeline(str(CONFIGS / config)).turn(
        "go", thread_id="b5", run_id="r1", tools=[ASK_USER]
    )
    call = _pending(r1)

    r2 = await Pipeline(str(CONFIGS / config)).turn(
        thread_id="b5",
        run_id="r2",
        tools=[ASK_USER],
        state=r1.state(),
        messages=_answer(r1, call.tool_call_id, '{"answers": ["Europe"]}'),
    )
    assert r2.finished() and not r2.errored()
    assert r2.run_outcome().pending_tool_call_ids is None
    assert "Coordinator done." in r2.text()
    assert not [
        e for e in r2.of_type(AGUIEventType.TOOL_CALL_START) if e.tool_call_name == "ask_user"
    ]


async def test_only_the_allowed_agents_receive_a_browser_tool() -> None:
    pipe = Pipeline(str(CONFIGS / "client_tools_delegate.yaml"))
    r1 = await pipe.turn("go", thread_id="b3", run_id="r1", tools=[ASK_USER])
    starts = r1.of_type(AGUIEventType.TOOL_CALL_START)
    assert [e.tool_call_name for e in starts if not e.subagent_run_id] == ["worker"]


class RecordingModel(ScriptedModel):
    """A scripted model that keeps the text of every request it receives."""

    seen: ClassVar[dict[str, list[str]]] = {}

    async def stream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        text = " ".join(
            block.get("text", "") for message in messages for block in message.get("content", [])
        )
        RecordingModel.seen.setdefault(str(self.get_config().get("model_id")), []).append(text)
        async for event in super().stream(messages, *args, **kwargs):
            yield event


A2UI_CATALOG = Context(
    description=A2UI_SCHEMA_CONTEXT_DESCRIPTION,
    value=json.dumps({"catalogId": "lab", "components": {"Text": {"type": "object"}}}),
)


async def test_a_delegate_renders_a2ui_without_waiting_and_corrects_a_malformed_surface() -> None:
    pipe = Pipeline(str(CONFIGS / "client_tools_a2ui.yaml"))
    r = await pipe.turn("go", thread_id="a1", run_id="r1", context=[A2UI_CATALOG])

    assert r.finished() and not r.errored()
    assert not r.run_outcome().pending_tool_call_ids
    renders = [
        e for e in r.of_type(AGUIEventType.TOOL_CALL_START) if e.tool_call_name == "render_a2ui"
    ]
    assert len(renders) == 2 and all(e.subagent_run_id for e in renders)
    results = {e.tool_call_id: e.content for e in r.of_type(AGUIEventType.TOOL_CALL_RESULT)}
    assert "no_root" in results[renders[0].tool_call_id]
    assert "Rendered surface churn" in results[renders[1].tool_call_id]
    assert "Coordinator done." in r.text()


async def test_the_entry_agent_renders_a2ui_and_goes_on() -> None:
    pipe = Pipeline(str(CONFIGS / "client_tools_a2ui_entry.yaml"))
    r = await pipe.turn(
        "total?",
        thread_id="a4",
        run_id="r1",
        context=[A2UI_CATALOG],
        forwarded_props={"injectA2UITool": True},
    )

    assert r.finished() and not r.errored()
    assert not r.run_outcome().pending_tool_call_ids
    [result] = [e.content for e in r.of_type(AGUIEventType.TOOL_CALL_RESULT)]
    assert "Rendered surface total" in result
    assert "Rendered the total." in r.text()


async def test_an_action_on_a_surface_reaches_the_agent_as_the_persons_turn() -> None:
    RecordingModel.seen.clear()
    pipe = Pipeline(str(CONFIGS / "a2ui_action.yaml"))
    r1 = await pipe.turn("show churn", thread_id="a3", run_id="r1")
    action = {
        "name": "break_down",
        "surfaceId": "churn",
        "sourceComponentId": "by-segment",
        "context": {"by": "segment"},
    }
    logged = [
        AssistantMessage(
            id="log",
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="log-call",
                    type="function",
                    function=FunctionCall(name="log_a2ui_event", arguments="{}"),
                )
            ],
        ),
        ToolMessage(
            id="log-result", role="tool", tool_call_id="log-call", content="User performed action"
        ),
    ]

    r2 = await pipe.turn(
        thread_id="a3",
        run_id="r2",
        state=r1.state(),
        messages=[*r1.messages(), *logged],
        forwarded_props={"a2uiAction": {"userAction": action}},
    )

    assert r2.finished() and not r2.errored()
    assert "Here it is by segment." in r2.text()
    assert (
        'I used "break_down" on the "churn" surface (by: segment).'
        in RecordingModel.seen["analyst"][-1]
    )
    assert "User performed action" not in RecordingModel.seen["analyst"][-1]


async def test_render_a2ui_and_its_catalog_reach_only_the_agents_allowed_it() -> None:
    RecordingModel.seen.clear()
    pipe = Pipeline(str(CONFIGS / "client_tools_context.yaml"))
    r = await pipe.turn("go", thread_id="a2", run_id="r1", tools=[ASK_USER], context=[A2UI_CATALOG])

    assert r.finished() and not r.errored()
    coordinator, worker = RecordingModel.seen["coordinator"][0], RecordingModel.seen["worker"][0]
    assert A2UI_SCHEMA_CONTEXT_DESCRIPTION not in coordinator
    assert A2UI_SCHEMA_CONTEXT_DESCRIPTION not in worker


async def test_context_reaches_every_agent_and_a_tools_context_only_its_agents() -> None:
    RecordingModel.seen.clear()
    pipe = Pipeline(str(CONFIGS / "client_tools_context.yaml"))
    r = await pipe.turn(
        "go",
        thread_id="b4",
        run_id="r1",
        tools=[ASK_USER],
        context=[
            Context(description="Open dashboard", value="d_1"),
            Context(description="tool:ask_user Ask with options", value="always offer two"),
        ],
    )
    assert r.finished() and not r.errored()
    coordinator, worker = RecordingModel.seen["coordinator"][0], RecordingModel.seen["worker"][0]
    assert "Open dashboard: d_1" in coordinator and "Open dashboard: d_1" in worker
    assert "always offer two" in worker and "always offer two" not in coordinator
