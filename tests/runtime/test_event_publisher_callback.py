"""EventPublisher's public callback-handler seam — TOKEN / REASONING / HANDOFF.

``as_callback_handler`` is public API (a strands-compatible callback_handler).
We drive it directly and observe the emitted StreamEvents — no private handlers.
"""

from __future__ import annotations

from types import SimpleNamespace

from kaboo_workflows.hooks import EventPublisher
from kaboo_workflows.hooks.event_publisher import _extract_incoming_task
from kaboo_workflows.types import EventType


def _publisher() -> tuple[EventPublisher, list]:
    events: list = []
    return EventPublisher(callback=events.append, agent_name="a"), events


def test_data_chunk_emits_token_event():
    pub, events = _publisher()
    pub.as_callback_handler()(data="hello")
    assert events[0].type == EventType.TOKEN
    assert events[0].data["text"] == "hello"


def test_reasoning_chunk_emits_reasoning_event():
    pub, events = _publisher()
    pub.as_callback_handler()(reasoningText="thinking")
    assert events[0].type == EventType.REASONING
    assert events[0].data["text"] == "thinking"


def test_empty_chunk_emits_nothing():
    pub, events = _publisher()
    pub.as_callback_handler()(data="")
    assert events == []


def test_multiagent_handoff_emits_handoff_event():
    pub, events = _publisher()
    pub.as_callback_handler()(
        type="multiagent_handoff", from_node_ids=["r"], to_node_ids=["w"], message="over to you"
    )
    assert events[0].type == EventType.HANDOFF
    assert events[0].data["to_node_ids"] == ["w"]
    assert events[0].data["message"] == "over to you"


def test_extract_task_plain_user_prompt():
    msgs = [{"role": "user", "content": [{"text": "Research the AI market"}]}]
    assert _extract_incoming_task(msgs) == "Research the AI market"


def test_extract_task_prefers_handoff_message_from_swarm_blob():
    blob = (
        "Context:\n"
        "Handoff Message: Please gather GPU pricing data.\n\n"
        "User Request: Research the AI infrastructure market\n\n"
        "Other agents available for collaboration:\nAgent name: writer.\n"
    )
    msgs = [{"role": "user", "content": [{"text": blob}]}]
    assert _extract_incoming_task(msgs) == "Please gather GPU pricing data."


def test_extract_task_falls_back_to_user_request_for_entry_node():
    blob = (
        "Context:\n"
        "User Request: Research the AI infrastructure market\n\n"
        "Other agents available for collaboration:\nAgent name: researcher.\n"
    )
    msgs = [{"role": "user", "content": [{"text": blob}]}]
    assert _extract_incoming_task(msgs) == "Research the AI infrastructure market"


def test_extract_task_ignores_tool_result_messages():
    msgs = [
        {"role": "user", "content": [{"text": "do the thing"}]},
        {"role": "user", "content": [{"toolResult": {"content": [{"text": "result"}]}}]},
    ]
    assert _extract_incoming_task(msgs) == "do the thing"


def test_extract_task_none_when_absent():
    assert _extract_incoming_task(None) is None
    assert _extract_incoming_task([{"role": "assistant", "content": [{"text": "hi"}]}]) is None


def test_callback_exception_is_swallowed_not_propagated():
    def _boom(_event):
        raise RuntimeError("consumer disconnected")

    pub = EventPublisher(callback=_boom, agent_name="a")
    # A RuntimeError in the consumer must not crash the producer.
    pub.as_callback_handler()(data="hi")


def _tool_end_event(*, status: str, exception=None) -> SimpleNamespace:
    return SimpleNamespace(
        tool_use={"name": "research_fetch_report", "toolUseId": "t1"},
        exception=exception,
        result={"status": status, "content": [{"text": "boom" if status == "error" else "ok"}]},
    )


def _tool_end(events: list):
    return next(e for e in events if e.type == EventType.TOOL_END)


def test_tool_end_marks_error_when_result_status_is_error():
    # An MCP tool that raises is caught by strands and returned as a ToolResult
    # with status="error" (no exception surfaces to the hook). We must still stamp
    # the streamed TOOL_END as "error" so the UI shows a failure, not "done".
    pub, events = _publisher()
    pub._on_tool_end(_tool_end_event(status="error"))
    assert _tool_end(events).data["status"] == "error"


def test_tool_end_marks_success_when_result_status_is_success():
    pub, events = _publisher()
    pub._on_tool_end(_tool_end_event(status="success"))
    assert _tool_end(events).data["status"] == "success"


def test_tool_end_marks_error_when_exception_present():
    pub, events = _publisher()
    pub._on_tool_end(_tool_end_event(status="success", exception=RuntimeError("x")))
    assert _tool_end(events).data["status"] == "error"
