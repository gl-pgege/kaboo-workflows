"""Interrupt hooks for human-in-the-loop tool gating.

``InterruptHook`` pauses agent execution before specified tool calls,
surfacing an approval prompt to the user via the AG-UI interrupt protocol.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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

    When ``ttl_seconds`` is set, the reason carries an ``expiresAt`` ISO-8601
    timestamp which the AG-UI adapter surfaces on the interrupt descriptor
    (``Interrupt.expiresAt``), letting clients render a countdown and servers
    expire unanswered approvals.

    An approval response may carry an edited ``tool_input`` (AG-UI
    ``approveWithEdits``): the gated call then executes with the user's
    arguments instead of the agent's — e.g. approving a subset of a bulk
    proposal. The edit applies to this one call only.
    """

    def __init__(
        self,
        tools: list[str],
        *,
        agent_name: str = "",
        ttl_seconds: int | None = None,
    ) -> None:
        self._tools = set(tools)
        self._agent_name = agent_name
        self._ttl_seconds = ttl_seconds

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use.get("name", "")
        if tool_name not in self._tools:
            return

        reason: dict[str, Any] = {
            "type": "approval",
            "message": f"Agent wants to call {tool_name}",
            "tool_name": tool_name,
            "tool_input": event.tool_use.get("input", {}),
        }
        if self._ttl_seconds is not None:
            expires = datetime.now(timezone.utc) + timedelta(seconds=self._ttl_seconds)
            reason["expiresAt"] = expires.isoformat().replace("+00:00", "Z")

        response = event.interrupt(
            name=f"{self._agent_name}:gate:{tool_name}",
            reason=reason,
        )
        if not response:
            return
        if response.get("status") == "cancelled":
            event.cancel_tool = "User rejected this action."
            return
        edited = response.get("tool_input")
        if isinstance(edited, dict):
            event.tool_use = {**event.tool_use, "input": edited}
