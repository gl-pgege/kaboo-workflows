"""Strands-interrupt → AG-UI descriptor mapping.

One canonical mapper shared by the single-agent path (``agui.py``) and the
multi-agent path (``_multiagent.py``), so every interrupt — whatever position
in the pipeline raised it — reaches the frontend with the same shape:
``id``, ``toolCallId`` (when tool-originated), ``reason``, ``message``,
``expiresAt`` (when the gate carries a TTL) and the raw ``metadata``.
"""

from __future__ import annotations

from typing import Any


def tool_call_id_from_interrupt_id(interrupt_id: Any) -> str | None:
    """Extract the originating tool-call id from a strands interrupt id.

    Interrupts raised from a ``BeforeToolCallEvent`` hook use the id
    ``v1:before_tool_call:<toolUseId>:<uuid>`` while interrupts raised from
    inside a tool via ``ToolContext.interrupt`` use ``v1:tool_call:<toolUseId>:
    <uuid>`` (see ``strands.hooks.events`` / ``strands.types.tools``). In both
    cases the tool-use id is the third segment. Forwarding it lets the frontend
    correlate the interrupt with its tool-call card so the answered Q&A can be
    rendered inline in the transcript.
    """
    parts = str(interrupt_id).split(":")
    if len(parts) >= 4 and parts[1] in ("before_tool_call", "tool_call"):
        return parts[2]
    return None


def map_strands_interrupt_to_agui(interrupt: Any) -> dict[str, Any]:
    """Translate a strands ``Interrupt`` into an AG-UI interrupt descriptor."""
    reason = getattr(interrupt, "reason", None)
    agui_interrupt: dict[str, Any] = {"id": getattr(interrupt, "id", None)}

    tool_call_id = tool_call_id_from_interrupt_id(agui_interrupt["id"])
    if tool_call_id:
        agui_interrupt["toolCallId"] = tool_call_id

    if isinstance(reason, dict):
        rtype = reason.get("type", "")
        if rtype == "approval":
            agui_interrupt["reason"] = "tool_call"
            agui_interrupt["message"] = reason.get("message", "Approval required")
        elif rtype == "form":
            questions = reason.get("questions", [])
            first = questions[0] if questions else None
            if isinstance(first, dict):
                first_q = str(first.get("question") or first.get("prompt") or "Input required")
            elif first is not None:
                first_q = str(first)
            else:
                first_q = "Input required"
            agui_interrupt["reason"] = "input_required"
            agui_interrupt["message"] = first_q
        else:
            agui_interrupt["reason"] = "confirmation"
            agui_interrupt["message"] = reason.get("message", str(reason))
        expires_at = reason.get("expiresAt")
        if isinstance(expires_at, str) and expires_at:
            agui_interrupt["expiresAt"] = expires_at
        agui_interrupt["metadata"] = reason
    else:
        message = str(reason) if reason else "Agent requires input"
        agui_interrupt["reason"] = "confirmation"
        agui_interrupt["message"] = message
        agui_interrupt["metadata"] = {"type": "approval", "message": message}

    return agui_interrupt
