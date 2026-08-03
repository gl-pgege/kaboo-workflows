"""MCPCallMetaHook — per-call ``_meta`` binding for MCP tools.

Drives the hook with a real ``BeforeToolCallEvent`` and a real
``MCPAgentTool`` (over a recording fake client) and asserts the observable
contract: the selected tool is swapped for a delegate whose call carries the
provider's metadata plus the stamped ``toolCallId``; non-MCP tools are
untouched.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from mcp.types import Tool as MCPTool
from strands import Agent, tool
from strands.hooks import HookRegistry
from strands.hooks.events import BeforeToolCallEvent
from strands.tools.mcp import MCPAgentTool, MCPClient
from strands.types.tools import ToolUse

from kaboo_workflows.hooks import MCPCallMetaHook
from kaboo_workflows.hooks.mcp_meta_hook import _MetaCallTool
from tests.fakes import FakeModel


class RecordingMCPClient:
    """Captures ``call_tool_async`` kwargs; returns a canned success result."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def call_tool_async(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "toolUseId": kwargs["tool_use_id"],
            "status": "success",
            "content": [{"text": "ok"}],
        }


def _mcp_tool(client: RecordingMCPClient) -> MCPAgentTool:
    spec = MCPTool(
        name="list_work_items",
        description="List work items",
        inputSchema={"type": "object", "properties": {}},
    )
    return MCPAgentTool(spec, cast(MCPClient, client))


def _tool_use(**input_args: Any) -> ToolUse:
    return {"toolUseId": "use-42", "name": "list_work_items", "input": input_args}


def _fire(hook: MCPCallMetaHook, selected_tool: Any, tool_use: ToolUse) -> BeforeToolCallEvent:
    agent = Agent(model=FakeModel())
    registry = HookRegistry()
    registry.add_hook(hook)
    event = BeforeToolCallEvent(
        agent=agent,
        selected_tool=selected_tool,
        tool_use=tool_use,
        invocation_state={},
    )
    registry.invoke_callbacks(event)
    return event


async def _drain(tool: Any, tool_use: ToolUse) -> None:
    async for _ in tool.stream(tool_use, {}):
        pass


@pytest.mark.asyncio
async def test_provider_meta_and_tool_call_id_ride_the_call() -> None:
    client = RecordingMCPClient()
    hook = MCPCallMetaHook(provider=lambda tool_use: {"runToken": "tok-1"})
    event = _fire(hook, _mcp_tool(client), _tool_use(limit="1"))

    assert event.selected_tool is not None
    assert event.selected_tool.tool_name == "list_work_items"
    await _drain(event.selected_tool, _tool_use(limit="1"))

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["meta"] == {"runToken": "tok-1", "toolCallId": "use-42"}
    assert call["name"] == "list_work_items"
    assert call["arguments"] == {"limit": "1"}


@pytest.mark.asyncio
async def test_provider_none_still_stamps_tool_call_id() -> None:
    client = RecordingMCPClient()
    event = _fire(MCPCallMetaHook(), _mcp_tool(client), _tool_use())
    await _drain(event.selected_tool, _tool_use())
    assert client.calls[0]["meta"] == {"toolCallId": "use-42"}


def test_provider_tool_call_id_wins_over_stamp() -> None:
    client = RecordingMCPClient()
    hook = MCPCallMetaHook(provider=lambda tool_use: {"toolCallId": "explicit"})
    event = _fire(hook, _mcp_tool(client), _tool_use())
    assert cast(_MetaCallTool, event.selected_tool)._meta["toolCallId"] == "explicit"  # noqa: SLF001


def test_no_meta_leaves_tool_untouched() -> None:
    client = RecordingMCPClient()
    original = _mcp_tool(client)
    event = _fire(
        MCPCallMetaHook(stamp_tool_call_id=False),
        original,
        _tool_use(),
    )
    assert event.selected_tool is original


def test_non_mcp_tools_are_untouched() -> None:
    @tool
    def local_tool() -> str:
        """A plain local tool."""
        return "hi"

    event = _fire(MCPCallMetaHook(), local_tool, _tool_use())
    assert event.selected_tool is local_tool


def test_provider_receives_the_tool_use() -> None:
    seen: list[ToolUse] = []

    def provider(tool_use: ToolUse) -> dict[str, Any]:
        seen.append(tool_use)
        return {}

    client = RecordingMCPClient()
    _fire(MCPCallMetaHook(provider=provider), _mcp_tool(client), _tool_use(a=1))
    assert seen == [{"toolUseId": "use-42", "name": "list_work_items", "input": {"a": 1}}]


def test_string_provider_resolves_via_import_spec() -> None:
    hook = MCPCallMetaHook(provider="tests.runtime.test_mcp_meta_hook:sample_provider")
    client = RecordingMCPClient()
    event = _fire(hook, _mcp_tool(client), _tool_use())
    assert cast(_MetaCallTool, event.selected_tool)._meta["marker"] == "from-spec"  # noqa: SLF001


def test_non_callable_provider_raises() -> None:
    with pytest.raises(TypeError, match="callable or an import spec"):
        MCPCallMetaHook(provider=42)


def sample_provider(tool_use: ToolUse) -> dict[str, Any]:
    """Import-spec target for ``test_string_provider_resolves_via_import_spec``."""
    return {"marker": "from-spec"}
