"""How a delegated agent's browser tool call waits for the browser.

The agent's stand-in parks the call in an ag-ui-strands frontend wait. Its
delegate forwards that wait to the coordinator as an interrupt with a
:data:`BROWSER_WAIT` reason naming the call, which every delegate above passes
on unchanged. The run reports the call in ``pendingToolCallIds``; when the
browser answers it with a tool message, that answer resumes the interrupt and
travels back down to the stand-in.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

BROWSER_WAIT = "kaboo:browser_tool"


def browser_wait_reason(tool_use_id: str) -> dict[str, str]:
    """The reason a forwarded browser wait carries."""
    return {"kaboo": "browser_tool", "tool_use_id": tool_use_id}


def browser_wait_call(reason: Any) -> str | None:
    """The browser tool call a forwarded wait is for, or ``None`` for any other interrupt."""
    if isinstance(reason, Mapping) and reason.get("kaboo") == "browser_tool":
        call = reason.get("tool_use_id")
        return call if isinstance(call, str) and call else None
    return None
