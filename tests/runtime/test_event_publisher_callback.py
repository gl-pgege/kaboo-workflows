"""EventPublisher's public callback-handler seam — TOKEN / REASONING / HANDOFF.

``as_callback_handler`` is public API (a strands-compatible callback_handler).
We drive it directly and observe the emitted StreamEvents — no private handlers.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

from kaboo_workflows.hooks import EventPublisher
from kaboo_workflows.hooks.event_publisher import _extract_incoming_task, _extract_result_text
from kaboo_workflows.types import EventType

if TYPE_CHECKING:
    from strands.hooks.events import AfterToolCallEvent


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


def _tool_end_event(*, status: str, exception=None) -> AfterToolCallEvent:
    return cast(
        "AfterToolCallEvent",
        SimpleNamespace(
            tool_use={"name": "research_fetch_report", "toolUseId": "t1"},
            exception=exception,
            result={
                "status": status,
                "content": [{"text": "boom" if status == "error" else "ok"}],
            },
        ),
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


def test_tool_end_streams_the_names_of_attachments_and_never_their_data():
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    data = base64.b64encode(png).decode()
    body = {
        "summary": "1 widget",
        "attachments": [{"name": "Rows 1-2", "mime_type": "image/png", "data": data}],
    }
    pub, events = _publisher()
    event = _tool_end_event(status="success")
    event.result["content"] = [{"text": json.dumps(body)}]
    pub._on_tool_end(event)
    streamed = _tool_end(events).data["tool_result"]
    assert '"summary": "1 widget"' in streamed
    assert "Rows 1-2" in streamed
    assert data not in streamed
    assert "attachments" not in streamed


def _text_result(text: str) -> dict:
    return {"status": "success", "content": [{"text": text}]}


def test_a_long_json_result_keeps_whole_rows_and_counts_the_rest():
    rows = [[n, f"customer-{n}", "north"] for n in range(2_000)]
    body = {
        "columns": ["id", "name", "region"],
        "untrusted_content": {"rows": rows},
        "page": {"next": "p2"},
    }

    preview = _extract_result_text(_text_result(json.dumps(body)), 16_000)

    assert preview is not None and len(preview) <= 16_000
    parsed = json.loads(preview)
    kept = parsed["untrusted_content"]["rows"]
    assert 0 < len(kept) < len(rows)
    assert kept == rows[: len(kept)]
    assert parsed["untrusted_content"]["_truncated"] == {"rows": len(rows) - len(kept)}
    assert parsed["columns"] == body["columns"]
    assert parsed["page"] == body["page"]


def test_a_long_json_block_result_is_previewed_as_json_too():
    rows = [[n, f"customer-{n}"] for n in range(2_000)]
    result = {"status": "success", "content": [{"json": {"columns": ["id", "name"], "rows": rows}}]}

    preview = _extract_result_text(result, 4_000)

    assert preview is not None and len(preview) <= 4_000
    parsed = json.loads(preview)
    assert parsed["rows"] == rows[: len(parsed["rows"])]
    assert parsed["_truncated"] == {"rows": len(rows) - len(parsed["rows"])}


def test_a_long_json_list_result_becomes_items_with_the_count_left_out():
    panels = [{"id": n, "title": f"panel-{n}"} for n in range(1_000)]

    preview = _extract_result_text(_text_result(json.dumps(panels)), 2_000)

    assert preview is not None and len(preview) <= 2_000
    parsed = json.loads(preview)
    kept = parsed["items"]
    assert 0 < len(kept) < len(panels)
    assert kept == panels[: len(kept)]
    assert parsed["_truncated"] == {"items": len(panels) - len(kept)}


def test_a_long_json_string_is_cut_with_an_ellipsis_and_its_count():
    content = "".join(chr(ord("a") + n % 26) for n in range(50_000))
    body = {"title": "Q3 report", "content": content}

    preview = _extract_result_text(_text_result(json.dumps(body)), 16_000)

    assert preview is not None and len(preview) <= 16_000
    parsed = json.loads(preview)
    kept = parsed["content"].removesuffix("…")
    assert parsed["content"].endswith("…")
    assert content.startswith(kept)
    assert parsed["_truncated"] == {"content": len(content) - len(kept)}
    assert parsed["title"] == "Q3 report"


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("a log line\n" * 5_000, id="text"),
        pytest.param(json.dumps({f"key-{n}": n for n in range(5_000)}), id="json-too-wide"),
        pytest.param("[" * 3_000 + "]" * 3_000, id="json-too-deep"),
    ],
)
def test_a_long_result_without_a_json_preview_keeps_the_plain_cut(raw: str):
    assert _extract_result_text(_text_result(raw), 1_000) == raw[:1_000] + "..."


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({"error": "Dashboard not found"}, "error", "Dashboard not found"),
        ({"status": "error", "message": "Dashboard not found"}, "error", "Dashboard not found"),
        ({"success": False, "reason": "Dashboard not found"}, "error", "Dashboard not found"),
        ({"error": None, "rows": []}, "success", None),
    ],
)
def test_tool_end_reads_a_failure_the_json_result_reports(body, status, error):
    pub, events = _publisher()
    event = _tool_end_event(status="success")
    event.result["content"] = [{"text": json.dumps(body)}]
    pub._on_tool_end(event)
    assert _tool_end(events).data["status"] == status
    assert _tool_end(events).data["error"] == error
