"""Approvals that tools ask for themselves (``interrupt.tool_approvals``).

A tool whose own service enforces approvals answers a call it will not run yet
with a ``kaboo_approval`` reply instead of a result. :class:`ToolApprovalHook`
turns that reply into the same approval request the gate raises, pauses the
run, and calls the tool once more when the person approves. The reply never
reaches the model, and the request ID comes only from it.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING, Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent
from strands.interrupt import InterruptException
from strands.types._events import ToolInterruptEvent, ToolResultEvent
from strands.types.tools import AgentTool, ToolContext, ToolGenerator, ToolSpec, ToolUse

from .interrupt_hook import APPROVAL_SCOPES, APPROVALS_STATE_KEY, _refusal, _tool_title

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

if TYPE_CHECKING:
    from collections.abc import Iterator

    from strands.types.tools import ToolResult

logger = logging.getLogger(__name__)

TOOL_APPROVAL_KEY = "kaboo_approval"
"""Key of the reply a tool sends instead of a result when a call needs approval."""

_ASK_USER = "ask_user"
_MAX_REQUEST_CHARS = 200
_MAX_TEXT_CHARS = 500
_STILL_NEEDS_APPROVAL = (
    "Still needs approval: the tool did not accept the approval, so this did not run."
)


def tool_approval_request(result: Any) -> dict[str, Any] | None:
    """Read a tool's ``kaboo_approval`` reply, if this result is one.

    The reply is a successful result whose ``structuredContent``, a ``json``
    content block, or a text block's JSON is an object with a
    ``kaboo_approval`` key; a gateway may keep only one of them. A reply a tool
    returned as a JSON string is read too, inside the ``{"result": ...}``
    object the MCP Python SDK's server wraps string results in. Error results
    never count, because gateways such as lcgw reduce an error to its text.

    Args:
        result: A strands tool result.

    Returns:
        ``{request, subject, scopes, message, expiresAt?}``, with ``once``
        always among the scopes and ``subject`` or ``message`` ``None`` when the
        tool gave none, or ``None`` when the result is not a usable reply.
    """
    if not isinstance(result, dict) or result.get("status") != "success":
        return None
    for candidate in _candidates(result):
        raw = candidate.get(TOOL_APPROVAL_KEY) if isinstance(candidate, dict) else None
        if isinstance(raw, dict):
            return _normalize(raw)
    return None


def _candidates(result: dict[str, Any]) -> Iterator[Any]:
    for value in _values(result):
        yield value
        wrapped = value.get("result") if isinstance(value, dict) else None
        if isinstance(wrapped, str):
            yield from _parsed(wrapped)


def _values(result: dict[str, Any]) -> Iterator[Any]:
    yield result.get("structuredContent")
    for block in result.get("content") or []:
        if not isinstance(block, dict):
            continue
        if "json" in block:
            yield block["json"]
        text = block.get("text")
        if isinstance(text, str):
            yield from _parsed(text)


def _parsed(text: str) -> Iterator[Any]:
    if TOOL_APPROVAL_KEY not in text:
        return
    try:
        yield json.loads(text)
    except ValueError:
        return


def _normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
    request = raw.get("request")
    if not isinstance(request, str) or not request.strip() or len(request) > _MAX_REQUEST_CHARS:
        logger.warning("an approval reply has no usable request id; passing it to the model")
        return None
    scopes = raw.get("scopes")
    offered = {s for s in scopes if s in APPROVAL_SCOPES} if isinstance(scopes, list) else set()
    normalized: dict[str, Any] = {
        "request": request,
        "subject": _subject(raw.get("subject")),
        "scopes": [s for s in APPROVAL_SCOPES if s == "once" or s in offered],
        "message": _text(raw.get("message")),
    }
    expires_at = raw.get("expiresAt")
    if isinstance(expires_at, str) and expires_at:
        normalized["expiresAt"] = expires_at
    return normalized


def _subject(value: Any) -> dict[str, str] | None:
    if isinstance(value, str) and value:
        return {"key": value, "label": value}
    if isinstance(value, dict) and isinstance(value.get("key"), str) and value["key"]:
        return {"key": value["key"], "label": _text(value.get("label")) or value["key"]}
    return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_MAX_TEXT_CHARS]


class ToolApprovalHook(HookProvider):
    """Let tools ask the person to approve a call.

    Wraps each tool the agent calls, except ``ask_user``. When a tool replies
    with ``kaboo_approval`` instead of a result, the run pauses with an
    approval request: the same one the gate raises, plus the tool's
    ``request`` ID. The reply never reaches the model. On resume:

    - Approved: the tool is called once more, with the person's edits if any,
      and its result is returned. If it asks again, the model reads that the
      call still needs approval, so the agent cannot loop.
    - Denied, skipped or expired: the tool is not called again, and the model
      reads the same note as for a gated call.

    The host must tell the tool's service about the decision before the run
    continues (kaboo-runtime's ``onDecision``). The service, not the agent,
    decides whether the second call runs.
    """

    def __init__(self, *, agent_name: str = "") -> None:
        """Configure the hook.

        Args:
            agent_name: Agent name, used to name the approval request.
        """
        self._agent_name = agent_name

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._on_before_tool)

    def _on_before_tool(self, event: BeforeToolCallEvent) -> None:
        tool = event.selected_tool
        if tool is None or event.cancel_tool or isinstance(tool, _ApprovalAwareTool):
            return
        if event.tool_use.get("name") == _ASK_USER:
            return
        event.selected_tool = _ApprovalAwareTool(tool, agent_name=self._agent_name)


class _ApprovalAwareTool(AgentTool):
    """Delegate that pauses the run when the wrapped tool asks for approval."""

    def __init__(self, inner: AgentTool, *, agent_name: str) -> None:
        super().__init__()
        self._inner = inner
        self._agent_name = agent_name

    @property
    def mcp_tool(self) -> Any:
        """The wrapped MCP tool definition, so its annotations stay visible."""
        return getattr(self._inner, "mcp_tool", None)

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
        agent = invocation_state.get("agent")
        if agent is None:
            async for event in self._inner.stream(tool_use, invocation_state, **kwargs):
                yield event
            return
        context = ToolContext(tool_use=tool_use, agent=agent, invocation_state=invocation_state)
        name = f"{self._agent_name}:tool_approval:{tool_use.get('name', '')}"
        answer = _stored_answer(context, name)
        if answer is not None:
            async for event in self._resume(tool_use, invocation_state, answer, **kwargs):
                yield event
            return

        result: ToolResultEvent | None = None
        async for event in self._inner.stream(tool_use, invocation_state, **kwargs):
            if isinstance(event, ToolResultEvent):
                result = event
                break
            yield event
            if isinstance(event, ToolInterruptEvent):
                return
        if result is None:
            return
        request = tool_approval_request(result.tool_result)
        if request is None:
            yield result
            return
        try:
            answer = context.interrupt(name, reason=self._reason(tool_use, request))
        except InterruptException as error:
            yield ToolInterruptEvent(tool_use, [error.interrupt])
            return
        async for event in self._resume(tool_use, invocation_state, answer, **kwargs):
            yield event

    def _reason(self, tool_use: ToolUse, request: dict[str, Any]) -> dict[str, Any]:
        tool_name = tool_use.get("name", "")
        reason: dict[str, Any] = {
            "type": "approval",
            "message": request["message"] or f"{tool_name} needs your approval",
            "tool_name": tool_name,
            "tool_input": tool_use.get("input", {}),
            "subject": request["subject"]
            or {"key": tool_name, "label": _tool_title(self._inner) or tool_name},
            "scopes": request["scopes"],
            "request": request["request"],
        }
        if "expiresAt" in request:
            reason["expiresAt"] = request["expiresAt"]
        return reason

    async def _resume(
        self,
        tool_use: ToolUse,
        invocation_state: dict[str, Any],
        answer: Any,
        **kwargs: Any,
    ) -> ToolGenerator:
        tool_use_id = str(tool_use.get("toolUseId", ""))
        if not (isinstance(answer, dict) and answer.get("status") == "approved"):
            decision, note, reason = _refusal(answer)
            record: dict[str, Any] = {"decision": decision, "by": "person"}
            if reason:
                record["reason"] = reason
            _record(invocation_state, tool_use_id, record)
            yield ToolResultEvent(_error(tool_use_id, note))
            return

        scope = answer.get("scope")
        _record(
            invocation_state,
            tool_use_id,
            {
                "decision": "approved",
                "by": "person",
                "scope": scope if scope in APPROVAL_SCOPES else "once",
            },
        )
        edited = answer.get("tool_input")
        call: ToolUse = {**tool_use, "input": edited} if isinstance(edited, dict) else tool_use
        async for event in self._inner.stream(call, invocation_state, **kwargs):
            if isinstance(event, ToolResultEvent):
                if tool_approval_request(event.tool_result) is not None:
                    yield ToolResultEvent(_error(tool_use_id, _STILL_NEEDS_APPROVAL))
                else:
                    yield event
                return
            yield event
            if isinstance(event, ToolInterruptEvent):
                return


def _stored_answer(context: ToolContext, name: str) -> Any:
    """The person's answer to this call's approval request, when the call is resuming.

    DEPENDS ON strands internal: ``Agent._interrupt_state``.
    """
    state = getattr(context.agent, "_interrupt_state", None)
    interrupts = getattr(state, "interrupts", None)
    if not isinstance(interrupts, dict):
        return None
    return getattr(interrupts.get(context._interrupt_id(name)), "response", None)


def _record(invocation_state: dict[str, Any], tool_use_id: str, record: dict[str, Any]) -> None:
    """Note how this call was decided, for its activity entry (see EventPublisher)."""
    invocation_state.setdefault(APPROVALS_STATE_KEY, {})[tool_use_id] = record


def _error(tool_use_id: str, text: str) -> ToolResult:
    return {"toolUseId": tool_use_id, "status": "error", "content": [{"text": text}]}
