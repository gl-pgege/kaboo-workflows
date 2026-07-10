"""Interrupt hooks for human-in-the-loop tool gating.

``InterruptHook`` pauses agent execution before specified tool calls,
surfacing an approval prompt to the user via the AG-UI interrupt protocol.
"""

from __future__ import annotations

import sys
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override


class InterruptHook(HookProvider):
    """Gate specific tool calls with user approval via strands interrupts.

    When the agent attempts to call a tool whose name is in the configured
    ``tools`` list, this hook fires ``event.interrupt()`` with a structured
    reason payload.  The agent pauses until the user approves or rejects.
    """

    def __init__(self, tools: list[str], *, agent_name: str = "") -> None:
        self._tools = set(tools)
        self._agent_name = agent_name

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "")
        if tool_name not in self._tools:
            return

        response = event.interrupt(
            name=f"{self._agent_name}:gate:{tool_name}",
            reason={
                "type": "approval",
                "message": f"Agent wants to call {tool_name}",
                "tool_name": tool_name,
                "tool_input": event.tool_use.get("input", {}),
            },
        )
        if response and response.get("status") == "cancelled":
            event.cancel_tool = "User rejected this action."
