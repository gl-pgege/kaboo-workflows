"""Per-call MCP ``_meta`` stamping for shared MCP clients.

``MCPCallMetaHook`` computes MCP call metadata (``params._meta``) in the
*caller's* request context — where per-request contextvars (the inbound
:class:`~kaboo_workflows._context.Principal`, application-bound run state, …)
are visible — and binds it to the outgoing ``tools/call``.

This complements the outbound auth strategies in
:mod:`kaboo_workflows.auth`: header-based strategies resolve on the MCP
transport's writer task, whose context is snapshotted once when a client
starts, so they cannot carry per-request identity over a long-lived shared
client. Metadata attached here rides the JSON-RPC message itself, so it is
always request-accurate regardless of which task performs the HTTP send.

The hook also stamps ``toolCallId`` (the strands ``toolUseId``) by default,
giving servers a stable idempotency/correlation key per tool invocation.
"""

from __future__ import annotations

import sys
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent
from strands.tools.mcp import MCPAgentTool
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool, ToolGenerator, ToolSpec, ToolUse

from ..utils import load_object

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


class MCPCallMetaHook(HookProvider):
    """Attach per-call ``_meta`` to every MCP tool call an agent makes.

    On :class:`~strands.hooks.events.BeforeToolCallEvent` — which fires in the
    caller's request context — the hook computes the metadata for this call and
    swaps ``event.selected_tool`` for a delegate that forwards it via
    ``MCPClient.call_tool_async(meta=...)`` (the documented tool-replacement
    seam of ``BeforeToolCallEvent``). Non-MCP tools are untouched.

    Args:
        provider: Callable ``(tool_use) -> dict | None`` returning metadata for
            one call, or an import spec string (``module.path:name`` /
            ``./file.py:name``) resolved to such a callable. ``None`` stamps
            only ``toolCallId``.
        stamp_tool_call_id: When ``True`` (default), ``toolCallId`` is set to
            the strands ``toolUseId`` unless the provider already supplied one.
    """

    def __init__(
        self,
        provider: Any | None = None,
        *,
        stamp_tool_call_id: bool = True,
    ) -> None:
        if isinstance(provider, str):
            provider = load_object(provider, target="MCP call-meta provider")
        if provider is not None and not callable(provider):
            raise TypeError(
                f"MCPCallMetaHook provider must be callable or an import spec, "
                f"got {type(provider).__name__}."
            )
        self._provider = provider
        self._stamp_tool_call_id = stamp_tool_call_id

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        tool = event.selected_tool
        if not isinstance(tool, MCPAgentTool):
            return
        meta: dict[str, Any] = {}
        if self._provider is not None:
            meta.update(self._provider(event.tool_use) or {})
        if self._stamp_tool_call_id:
            meta.setdefault("toolCallId", event.tool_use.get("toolUseId"))
        if meta:
            event.selected_tool = _MetaCallTool(tool, meta)


class _MetaCallTool(AgentTool):
    """Delegate that calls the wrapped MCP tool with bound ``_meta``.

    Metadata is computed once per call (in the caller's context) and captured
    here, so the actual HTTP send — which happens on the MCP client's
    background task — carries it without any context propagation.
    """

    def __init__(self, inner: MCPAgentTool, meta: dict[str, Any]) -> None:
        super().__init__()
        self._inner = inner
        self._meta = meta

    @property
    @override
    def tool_name(self) -> str:
        return self._inner.tool_name

    @property
    @override
    def tool_spec(self) -> ToolSpec:
        return self._inner.tool_spec

    @property
    @override
    def tool_type(self) -> str:
        return self._inner.tool_type

    @override
    async def stream(
        self, tool_use: ToolUse, invocation_state: dict[str, Any], **kwargs: Any
    ) -> ToolGenerator:
        result = await self._inner.mcp_client.call_tool_async(
            tool_use_id=tool_use["toolUseId"],
            name=self._inner.mcp_tool.name,
            arguments=tool_use["input"],
            read_timeout_seconds=self._inner.timeout,
            meta=self._meta,
        )
        yield ToolResultEvent(result)
