"""Reading a tool's ``kaboo_approval`` reply, and which tools the hook wraps."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from strands.hooks.events import BeforeToolCallEvent
from strands.types.tools import AgentTool

from kaboo_workflows.hooks import TOOL_APPROVAL_KEY, ToolApprovalHook
from kaboo_workflows.hooks.tool_approval_hook import _ApprovalAwareTool, tool_approval_request

REPLY = {"request": "req-9", "subject": {"key": "store:a/catalog", "label": "catalog in A"}}


def _text(payload: Any) -> dict[str, Any]:
    return {"status": "success", "content": [{"text": json.dumps(payload)}]}


@pytest.mark.parametrize(
    "result",
    [
        _text({TOOL_APPROVAL_KEY: REPLY}),
        {"status": "success", "content": [{"json": {TOOL_APPROVAL_KEY: REPLY}}]},
        {
            "status": "success",
            "content": [{"text": "needs approval"}],
            "structuredContent": {TOOL_APPROVAL_KEY: REPLY},
        },
        {
            "status": "success",
            "content": [{"text": json.dumps({"result": json.dumps({TOOL_APPROVAL_KEY: REPLY})})}],
            "structuredContent": {"result": json.dumps({TOOL_APPROVAL_KEY: REPLY})},
        },
        _text({"result": json.dumps({TOOL_APPROVAL_KEY: REPLY})}),
    ],
)
def test_the_reply_is_read_from_text_json_or_structured_content(result: dict[str, Any]) -> None:
    request = tool_approval_request(result)

    assert request == {
        "request": "req-9",
        "subject": {"key": "store:a/catalog", "label": "catalog in A"},
        "scopes": ["once"],
        "message": None,
    }


@pytest.mark.parametrize(
    "result",
    [
        {"status": "error", "content": [{"text": json.dumps({TOOL_APPROVAL_KEY: REPLY})}]},
        _text({TOOL_APPROVAL_KEY: {**REPLY, "request": ""}}),
        _text({TOOL_APPROVAL_KEY: {**REPLY, "request": "x" * 201}}),
        _text({TOOL_APPROVAL_KEY: {"subject": "store:a"}}),
        {"status": "success", "content": [{"text": "not json, but mentions kaboo_approval"}]},
        _text({"other": REPLY}),
        "kaboo_approval",
    ],
)
def test_anything_else_is_not_a_reply(result: Any) -> None:
    assert tool_approval_request(result) is None


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (None, ["once"]),
        (["thread", "bogus"], ["once", "thread"]),
        (["always", "thread"], ["once", "thread", "always"]),
    ],
)
def test_once_is_always_offered_and_scopes_keep_their_order(
    scopes: Any, expected: list[str]
) -> None:
    request = tool_approval_request(_text({TOOL_APPROVAL_KEY: {**REPLY, "scopes": scopes}}))

    assert request is not None and request["scopes"] == expected


def test_subject_message_and_deadline_are_normalized() -> None:
    reply = {
        "request": "req-9",
        "subject": "store:a/catalog",
        "message": "  Change prices  ",
        "expiresAt": "2030-01-01T00:00:00Z",
    }

    request = tool_approval_request(_text({TOOL_APPROVAL_KEY: reply}))

    assert request == {
        "request": "req-9",
        "subject": {"key": "store:a/catalog", "label": "store:a/catalog"},
        "scopes": ["once"],
        "message": "Change prices",
        "expiresAt": "2030-01-01T00:00:00Z",
    }


def _event(name: str, tool: Any, cancel: Any = False) -> BeforeToolCallEvent:
    return cast(
        BeforeToolCallEvent,
        SimpleNamespace(tool_use={"name": name}, selected_tool=tool, cancel_tool=cancel),
    )


def test_the_hook_wraps_tools_but_not_cancelled_calls_or_itself() -> None:
    hook = ToolApprovalHook(agent_name="a")
    tool = SimpleNamespace(tool_name="set_price")

    wrapped = _event("set_price", tool)
    hook._on_before_tool(wrapped)
    assert isinstance(wrapped.selected_tool, _ApprovalAwareTool)

    again = _event("set_price", wrapped.selected_tool)
    hook._on_before_tool(again)
    assert again.selected_tool is wrapped.selected_tool

    for event in (
        _event("set_price", tool, cancel="no"),
        _event("x", None),
    ):
        hook._on_before_tool(event)
        assert event.selected_tool is tool or event.selected_tool is None


def test_the_wrapper_keeps_the_tools_mcp_annotations_visible() -> None:
    annotations = SimpleNamespace(readOnlyHint=False, destructiveHint=True)
    inner = SimpleNamespace(
        tool_name="run_mutation", mcp_tool=SimpleNamespace(annotations=annotations)
    )

    wrapper = _ApprovalAwareTool(cast(AgentTool, inner), agent_name="a")

    assert wrapper.mcp_tool.annotations is annotations
    assert wrapper.tool_name == "run_mutation"
