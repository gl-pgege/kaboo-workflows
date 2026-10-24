"""Strands interrupts ↔ AG-UI, on top of ag-ui-strands' native interrupts.

ag-ui-strands publishes every interrupt a run pauses on and applies the
client's ``resume`` entries itself. kaboo adds what only it knows:

- :func:`lift_interrupt` reads kaboo's own reason off a published interrupt —
  ``kaboo:approval`` — and fills the standard fields
  from it: the tool call it is about (a delegate's own call, not the one that
  delegated to it), the sub-agent run that made that call, the message and the
  answer's schema. kaboo's fields stay in ``metadata``.
- :func:`browser_resume_entries` turns the browser's answers to delegated
  agents' browser tools into resume entries for the interrupts carrying them.
- :func:`approval_descriptors` describes the approvals still waiting, for the
  host (``kaboo_sessions.approvals``).

A deadline stays in ``metadata.expiresAt`` and is never copied to AG-UI's own
``expiresAt``: the AG-UI client refuses to resume a thread, or to send anything
on it, once that field has passed. The gate enforces the deadline itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ag_ui.core import Interrupt, ResumeEntry
from ag_ui_strands.frontend_tool_interrupt import wrap_frontend_tool_response

from ..tools._browser import browser_wait_call

APPROVAL = "kaboo:approval"
"""The reason an approval request is published with."""

APPROVAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"enum": ["approved", "cancelled"]},
        "scope": {"enum": ["once", "thread", "always"]},
        "reason": {"type": "string", "description": "Why the person declined."},
        "tool_input": {
            "type": "object",
            "description": "The arguments the person approved instead.",
        },
    },
    "required": ["status"],
}
"""What an approval's answer may hold. ``{"cancelled": true}`` resume entries are skips."""

_SKIPPED_BROWSER_CALL = "Skipped: the person moved on without answering."


def tool_call_id_from_interrupt_id(interrupt_id: Any) -> str | None:
    """Extract the originating tool-call id from a strands interrupt id.

    Interrupts raised from a ``BeforeToolCallEvent`` hook use the id
    ``v1:before_tool_call:<toolUseId>:<uuid>`` while interrupts raised from
    inside a tool via ``ToolContext.interrupt`` use ``v1:tool_call:<toolUseId>:
    <uuid>``. In both cases the tool-use id is the third segment.
    """
    parts = str(interrupt_id).split(":")
    if len(parts) >= 4 and parts[1] in ("before_tool_call", "tool_call"):
        return parts[2]
    return None


def kaboo_reason(interrupt: Any) -> dict[str, Any] | None:
    """The reason kaboo raised a published interrupt with, when kaboo raised it."""
    metadata = getattr(interrupt, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("reason", metadata)
    return raw if isinstance(raw, dict) else None


def lift_interrupt(interrupt: Interrupt, run_of_call: Callable[[str], str | None]) -> Interrupt:
    """Fill a published interrupt's standard fields from kaboo's reason.

    Args:
        interrupt: The interrupt as ag-ui-strands published it.
        run_of_call: The sub-agent run that made a tool call, by its id.

    Returns:
        The interrupt to send, unchanged when kaboo did not raise it.
    """
    reason = kaboo_reason(interrupt)
    if reason is None:
        return interrupt
    kind = reason.get("type")
    if kind == "approval":
        call = reason.get("tool_use_id") or tool_call_id_from_interrupt_id(interrupt.id)
        update: dict[str, Any] = {
            "reason": APPROVAL,
            "message": reason.get("message") or "Approval required",
            "response_schema": APPROVAL_RESPONSE_SCHEMA,
            "metadata": reason,
        }
        if isinstance(call, str):
            update["tool_call_id"] = call
            if run := run_of_call(call):
                update["subagent_run_id"] = run
        return interrupt.model_copy(update=update)
    return interrupt.model_copy(update={"metadata": reason})


def browser_resume_entries(
    pending: Mapping[str, Any], answers: Mapping[str, tuple[str, bool]]
) -> list[ResumeEntry]:
    """Resume entries for the interrupts carrying delegated agents' browser tool calls.

    Every such call gets an entry — the browser's answer, or a note that the
    person moved on — because a resume must address every open interrupt.

    Args:
        pending: The paused agent's unresolved strands interrupts, by id.
        answers: The browser's answers by call id: ``(content, is_error)``.
    """
    entries: list[ResumeEntry] = []
    for interrupt_id, interrupt in pending.items():
        call = browser_wait_call(getattr(interrupt, "reason", None))
        if call is None:
            continue
        content, is_error = answers.get(call, (_SKIPPED_BROWSER_CALL, True))
        entries.append(
            ResumeEntry(
                interrupt_id=interrupt_id,
                status="resolved",
                payload=wrap_frontend_tool_response(content, is_error=is_error),
            )
        )
    return entries


def approval_descriptors(interrupts: Iterable[Any]) -> list[dict[str, Any]]:
    """Describe the approvals an agent is waiting on, for the host.

    Written to ``kaboo_sessions.approvals`` so a host can match the person's
    decisions to what the agent actually asked — tool, subject, offered scopes
    and the proposed input, plus the ``request`` ID when a tool asked for the
    approval itself — without reading strands state.

    Args:
        interrupts: The agent's unresolved strands interrupts.

    Returns:
        One descriptor per approval request, in order.
    """
    descriptors: list[dict[str, Any]] = []
    for interrupt in interrupts:
        reason = getattr(interrupt, "reason", None)
        if not isinstance(reason, dict) or reason.get("type") != "approval":
            continue
        tool = reason.get("tool_name")
        descriptor: dict[str, Any] = {
            "interruptId": interrupt.id,
            "toolCallId": reason.get("tool_use_id") or tool_call_id_from_interrupt_id(interrupt.id),
            "tool": tool,
            "subject": reason.get("subject") or {"key": tool, "label": tool},
            "scopes": reason.get("scopes") or ["once"],
            "input": reason.get("tool_input"),
        }
        if reason.get("expiresAt"):
            descriptor["expiresAt"] = reason["expiresAt"]
        if isinstance(reason.get("request"), str):
            descriptor["request"] = reason["request"]
        descriptors.append(descriptor)
    return descriptors
