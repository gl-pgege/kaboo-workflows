"""Strands interrupts ↔ AG-UI: descriptors out, resume answers in.

One canonical home shared by the single-agent path (``agui.py``) and the
multi-agent path (``_multiagent.py``), so every interrupt — whatever position
in the pipeline raised it — reaches the frontend with the same shape and every
answer is read back the same way:

- :func:`map_strands_interrupt_to_agui` builds the AG-UI descriptor: ``id``,
  ``toolCallId`` (when tool-originated), ``reason``, ``message`` and the raw
  ``metadata``.
- :func:`build_resume_responses` turns the client's ``resume`` entries into
  strands interrupt responses, enforcing deadlines and treating anything
  unanswered as skipped.
- :func:`approval_descriptors` describes the approvals still waiting, for the
  host (``kaboo_session.approvals``).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

SKIPPED: dict[str, Any] = {"status": "cancelled", "skipped": True}
"""Response for a request the person skipped or never answered.

``cancelled`` keeps tools written against the plain ``{"status": "cancelled"}``
contract working; ``skipped`` lets the gate tell a skip from a denial.
"""

EXPIRED: dict[str, Any] = {"status": "cancelled", "expired": True}
"""Response for a request answered after its ``expiresAt``."""


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
    """Translate a strands ``Interrupt`` into an AG-UI interrupt descriptor.

    A deadline stays in ``metadata.expiresAt`` and is never copied to AG-UI's
    own ``expiresAt``: the AG-UI client refuses to resume a thread, or to send
    anything on it, once that field has passed. The agent enforces the deadline
    itself (see :func:`build_resume_responses`).
    """
    reason = getattr(interrupt, "reason", None)
    agui_interrupt: dict[str, Any] = {"id": getattr(interrupt, "id", None)}

    tool_call_id = tool_call_id_from_interrupt_id(agui_interrupt["id"])
    if tool_call_id:
        agui_interrupt["toolCallId"] = tool_call_id

    if isinstance(reason, dict):
        rtype = reason.get("type", "")
        if rtype == "continuation":
            # Not a question: the run filled its response budget and stopped at
            # a tool boundary. The client resumes it immediately, with no
            # prompt, so the turn carries on in the next response.
            agui_interrupt["reason"] = "continuation"
            agui_interrupt["message"] = reason.get("message", "Continuing")
        elif rtype == "approval":
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
        agui_interrupt["metadata"] = reason
    else:
        message = str(reason) if reason else "Agent requires input"
        agui_interrupt["reason"] = "confirmation"
        agui_interrupt["message"] = message
        agui_interrupt["metadata"] = {"type": "approval", "message": message}

    return agui_interrupt


def _is_expired(reason: Any, now: datetime) -> bool:
    expires_at = reason.get("expiresAt") if isinstance(reason, dict) else None
    if not isinstance(expires_at, str) or not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("expires_at=<%s> | unreadable interrupt deadline; ignoring it", expires_at)
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now


def build_resume_responses(
    pending: dict[str, Any], resume_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map AG-UI resume entries onto the interrupts an agent is waiting on.

    A ``resolved`` entry passes its payload through (a bare one approves). A
    ``cancelled`` entry, and any waiting interrupt the client did not address,
    becomes :data:`SKIPPED` — never a silent approval. An interrupt whose
    deadline has passed answers :data:`EXPIRED`, whatever the client sent.

    Args:
        pending: The unresolved strands interrupts, keyed by id.
        resume_entries: The client's ``resume`` entries
            (``interruptId`` / ``status`` / ``payload``).

    Returns:
        ``[{"interruptResponse": {"interruptId", "response"}}, ...]`` — a valid
        strands resume input for an ``Agent`` or a ``MultiAgentBase``.
    """
    answers: dict[str, Any] = {}
    for entry in resume_entries:
        interrupt_id = entry.get("interruptId", "")
        if entry.get("status", "resolved") == "resolved":
            payload = entry.get("payload")
            answers[interrupt_id] = payload if payload is not None else {"status": "approved"}
        else:
            answers[interrupt_id] = dict(SKIPPED)

    now = datetime.now(timezone.utc)
    responses: list[dict[str, Any]] = []
    for interrupt_id, interrupt in pending.items():
        if interrupt_id in answers:
            response = answers[interrupt_id]
        else:
            logger.warning(
                "interrupt=<%s> | resume did not address it; treating it as skipped", interrupt_id
            )
            response = dict(SKIPPED)
        if _is_expired(getattr(interrupt, "reason", None), now):
            response = dict(EXPIRED)
        responses.append({"interruptResponse": {"interruptId": interrupt_id, "response": response}})
    return responses


def approval_descriptors(interrupts: Iterable[Any]) -> list[dict[str, Any]]:
    """Describe the approvals an agent is waiting on, for the host.

    Written to ``kaboo_session.approvals`` so a host can match the person's
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
            "toolCallId": tool_call_id_from_interrupt_id(interrupt.id),
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
