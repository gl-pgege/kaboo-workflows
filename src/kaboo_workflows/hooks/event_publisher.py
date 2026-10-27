"""EventPublisher hook for streaming agent activities to external consumers.

Key Features:
    - Unified single-agent and multi-agent event publishing
    - Safe callback wrapping that logs instead of propagating exceptions
    - Longest-prefix tool label resolution for display names
    - Callback handler factory for TOKEN, REASONING, and HANDOFF events
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import sys
from collections.abc import Callable, Iterator
from typing import Any

from strands.hooks import HookProvider, HookRegistry

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from strands.hooks.events import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterMultiAgentInvocationEvent,
    AfterNodeCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeMultiAgentInvocationEvent,
    BeforeNodeCallEvent,
    BeforeToolCallEvent,
)

from .._context import get_delegation_id, get_run_id, get_thread_id, get_turn_id
from ..telemetry import current_trace_id
from ..types import EventType, StreamEvent
from .cost_tracking import pop_cost_box, push_cost_box
from .interrupt_hook import APPROVALS_STATE_KEY
from .tool_attachments_hook import unpack_attachments

logger = logging.getLogger(__name__)

EventCallback = Callable[[StreamEvent], None]

_MAX_RESULT_LEN = 16_000
_MAX_ERROR_LEN = 500
_PREVIEW_ITEMS = (50, 20, 10, 5, 3, 1)
_PREVIEW_CHARS = (4000, 1000, 300)
_HTTP_ERROR = re.compile(r"HTTP error \d+: .*? - (.+)", re.DOTALL)


# ============================================================================
# Helpers
# ============================================================================


def _result_text(result: Any) -> str:
    """Join the text and json blocks of a strands tool result."""
    if result is None:
        return ""
    content = result.get("content", [])
    content = unpack_attachments(content) or content
    parts: list[str] = []
    for block in content:
        if "text" in block:
            parts.append(block["text"])
        elif "json" in block:
            parts.append(
                json.dumps(block["json"], ensure_ascii=False, separators=(",", ":"), default=str)
            )
    return "\n".join(parts)


def _extract_result_text(result: Any, max_len: int = _MAX_RESULT_LEN) -> str | None:
    """Extract a plain-text summary from a strands tool result (for streaming TOOL_END).

    Text over ``max_len`` is cut, except a JSON object or array, which is cut down
    to valid JSON that fits (see :func:`_json_preview`).
    """
    raw = _result_text(result)
    if not raw:
        return None
    if len(raw) <= max_len:
        return raw
    return _json_preview(raw, max_len) or raw[:max_len] + "..."


def _parse_json(text: str) -> Any:
    """Return ``text`` parsed as JSON, or ``None`` when it is not JSON."""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _json_preview(raw: str, max_len: int) -> str | None:
    """Return the JSON in ``raw`` re-serialized compactly within ``max_len``.

    Each pass cuts more: lists under a key to their first 50, 20, 10, 5, 3 or 1
    items, and strings to 4000, 1000 or 300 characters and an ellipsis. The dict
    holding a cut key records in ``_truncated`` how many items or characters it
    lost; a cut top-level list becomes ``{"items": [...], "_truncated": {"items": N}}``.
    ``None`` when ``raw`` is not a JSON object or array, or no pass fits.
    """
    value = _parse_json(raw)
    if not isinstance(value, (dict, list)):
        return None
    try:
        for preview in _previews(value):
            text = json.dumps(preview, ensure_ascii=False, separators=(",", ":"))
            if len(text) <= max_len:
                return text
    except RecursionError:
        return None
    return None


def _previews(value: Any) -> Iterator[Any]:
    """Yield ``value`` whole, then cut down further on each pass."""
    yield value
    for max_items in _PREVIEW_ITEMS:
        for max_chars in _PREVIEW_CHARS:
            if isinstance(value, list) and len(value) > max_items:
                yield {
                    "items": _shrink(value[:max_items], max_items, max_chars),
                    "_truncated": {"items": len(value) - max_items},
                }
            else:
                yield _shrink(value, max_items, max_chars)


def _shrink(value: Any, max_items: int, max_chars: int) -> Any:
    """Return ``value`` with lists under a key and strings cut, cuts recorded in ``_truncated``."""
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[:max_chars] + "…"
    if isinstance(value, list):
        return [_shrink(item, max_items, max_chars) for item in value]
    if not isinstance(value, dict):
        return value
    shrunk: dict[str, Any] = {}
    omitted: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, list) and len(item) > max_items:
            omitted[key] = len(item) - max_items
            item = item[:max_items]
        elif isinstance(item, str) and len(item) > max_chars:
            omitted[key] = len(item) - max_chars
        shrunk[key] = _shrink(item, max_items, max_chars)
    if omitted:
        shrunk["_truncated"] = omitted
    return shrunk


def _result_failure(result: Any) -> dict[str, str] | None:
    """Return the ``error`` (and ``fix``) a failed strands tool result reports, or ``None``.

    A result failed when strands marks it ``status: "error"`` or its JSON says so
    at the top level: an ``errorMessage`` (how the MCP gateway reports an HTTP
    failure), an ``error``, ``status: "error"`` or ``success: false``. The error
    is the most specific message found, at most 500 characters.
    """
    if not isinstance(result, dict):
        return None
    text = _result_text(result)
    body = _parse_json(text)
    if not isinstance(body, dict):
        body = {}
    if result.get("status") != "error" and not (
        body.get("errorMessage")
        or body.get("error")
        or body.get("status") == "error"
        or body.get("success") is False
    ):
        return None
    error_message = str(body.get("errorMessage") or "")
    match = _HTTP_ERROR.match(error_message)
    detail = (match.group(1) if match else error_message).strip()
    inner = _literal_dict(detail)
    message = (
        _first_text(inner, "message")
        or detail
        or _first_text(body, "message", "error", "reason")
        or text.strip()
    )
    failure = {"error": message[:_MAX_ERROR_LEN]}
    fix = inner.get("fix") or body.get("fix")
    if fix:
        failure["fix"] = str(fix)
    return failure


def tool_outcome(event: AfterToolCallEvent) -> dict[str, str]:
    """Return how a tool call ended: its ``status`` and, when it failed, ``error`` and ``fix``."""
    failure = _result_failure(event.result) or {}
    if event.exception:
        failure["error"] = str(event.exception)[:_MAX_ERROR_LEN]
    return {"status": "error" if failure else "success", **failure}


def _literal_dict(text: str) -> dict[str, Any]:
    """Return ``text`` read as a Python dict literal, or ``{}`` when it is not one."""
    if not text.startswith("{"):
        return {}
    try:
        value = ast.literal_eval(text)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _first_text(values: dict[str, Any], *keys: str) -> str:
    """Return the first non-blank string under ``keys`` in ``values``, stripped."""
    for key in keys:
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _resolve_tool_label(
    tool_name: str,
    labels: dict[str, str] | None = None,
) -> str | None:
    """Resolve a tool name to a display label via exact or longest-prefix match."""
    if not labels:
        return None
    if tool_name in labels:
        return labels[tool_name]
    best_match = None
    best_length = 0
    for prefix, label in labels.items():
        if tool_name.startswith(prefix) and len(prefix) > best_length:
            best_match = label
            best_length = len(prefix)
    return best_match


def _safe_callback(callback: EventCallback) -> EventCallback:
    """Wrap *callback* so exceptions are logged instead of propagated."""

    def _wrapper(event: StreamEvent) -> None:
        try:
            callback(event)
        except (RuntimeError, OSError, asyncio.QueueFull):
            logger.warning(
                "hook=<%s> | event callback raised an exception", "EventPublisher", exc_info=True
            )
        except Exception as e:
            logger.error(
                "hook=<%s> | event callback raised an unexpected exception: %s: %s",
                "EventPublisher",
                type(e).__name__,
                e,
                exc_info=True,
            )
            raise

    return _wrapper


def _extract_incoming_task(messages: Any, max_len: int = 400) -> str | None:
    """Return the task an agent was handed, as a clean human-readable line.

    Scans messages newest-to-oldest for a ``user``-role message that carries
    text and is not purely a tool result (strands appends tool results as
    user-role messages). For the entry agent this is the user's prompt; for a
    swarm node the text is a labeled context blob, so we pull out the most
    relevant piece:

    - the ``Handoff Message:`` (what the previous agent asked this one to do), or
    - the ``User Request:`` (the original task), or
    - the raw text otherwise.

    Returns ``None`` when nothing suitable is found so the card omits the task.
    """
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            blocks: list[Any] = [{"text": content}]
        elif isinstance(content, list):
            blocks = content
        else:
            continue
        has_tool_result = any(isinstance(b, dict) and "toolResult" in b for b in blocks)
        if has_tool_result:
            continue
        texts: list[str] = []
        for b in blocks:
            if isinstance(b, dict):
                block_text = b.get("text")
                if isinstance(block_text, str):
                    texts.append(block_text)
        text = "\n".join(t for t in texts if t.strip()).strip()
        if not text:
            continue
        task = _pick_task_line(text)
        return task if len(task) <= max_len else task[: max_len - 1].rstrip() + "…"
    return None


def _pick_task_line(text: str) -> str:
    """Pull the most relevant task out of a (possibly labeled) message body.

    Prefers a swarm ``Handoff Message:`` section, then ``User Request:``; falls
    back to the whole text. Each labeled section runs until the next blank line.
    """
    for label in ("Handoff Message:", "User Request:"):
        idx = text.find(label)
        if idx == -1:
            continue
        section = text[idx + len(label) :]
        section = section.split("\n\n", 1)[0]
        section = section.strip()
        if section:
            return section
    return text.strip()


# ============================================================================
# EventPublisher
# ============================================================================


class EventPublisher(HookProvider):
    """Unified event publisher for single-agent and multi-agent orchestrations."""

    def __init__(
        self,
        callback: EventCallback,
        agent_name: str,
        *,
        tool_labels: dict[str, str] | None = None,
        stream_group: str = "",
        stream_title: str = "",
        is_chat_reply: bool = False,
        max_result_len: int = _MAX_RESULT_LEN,
    ) -> None:
        """Initialize the EventPublisher.

        Converts strands hook events into :class:`StreamEvent` objects and
        delivers them to an external callback.  Emits an AGENT_COMPLETE event at
        the end of each invocation with usage metrics from ``EventLoopMetrics``.

        For TOKEN and REASONING events use :meth:`as_callback_handler` to
        create a strands-compatible ``callback_handler``.

        Args:
            callback: Called with each :class:`StreamEvent`.
            agent_name: Identifier for the agent or orchestrator.
            tool_labels: Optional mapping of tool names to display labels.
            stream_group: Dot-path stream group for hierarchical activity
                attribution. Empty string when not configured.
            stream_title: Human-readable title for the stream group.
            max_result_len: Maximum character length for tool result text
                in TOOL_END events; longer JSON is cut down to valid JSON
                that records what it left out in ``_truncated``. Default: 16,000.

        Example::

            publisher = EventPublisher(callback=on_event, agent_name="analyzer")
            agent = Agent(
                hooks=[publisher],
                callback_handler=publisher.as_callback_handler(),
            )
        """
        self._callback = _safe_callback(callback)
        self._agent_name = agent_name
        self._tool_labels = tool_labels or {}
        self._stream_group = stream_group
        self._stream_title = stream_title
        self._is_chat_reply = is_chat_reply
        self._max_result_len = max_result_len
        # Set for the plain-agent entry node: its text and tool calls are already
        # rendered inline in the chat by the host (CopilotKit), so the activity
        # group exists only to enrich those tool rows — it must never also render
        # as a drill card. The AG-UI adapter flips this on when forwarding this
        # publisher to the ag-ui-strands clone. See create_agui_app.
        self._inline_chat_owner = False
        # Per-conversation state. A single EventPublisher instance is shared across
        # every concurrent thread that runs this agent (delegate nodes and the
        # forwarded plain-agent entry are singletons), so all per-invocation state
        # — the ``#N`` suffix, active group, and error latch — must be scoped by
        # thread_id to avoid cross-talk between concurrent conversations.
        self._invocation_count_by_thread: dict[str | None, int] = {}
        self._active_group_by_thread: dict[str | None, str] = {}
        self._errored_by_thread: dict[str | None, bool] = {}

    def mark_inline_chat_owner(self) -> None:
        """Flag this publisher's group as the inline chat owner (see ``__init__``)."""
        self._inline_chat_owner = True

    def _enrich(self, data: dict[str, Any]) -> dict[str, Any]:
        """Inject stream_group, agent_name, and conversation ids into event data."""
        thread_id = get_thread_id()
        active_group = self._active_group_by_thread.get(thread_id, self._stream_group)
        if active_group:
            data["stream_group"] = active_group
            data["stream_title"] = self._stream_title
        data["agent_name"] = self._agent_name
        if self._inline_chat_owner:
            data["inline_chat_owner"] = True
        data["thread_id"] = thread_id
        data["run_id"] = get_run_id()
        data["turn_id"] = get_turn_id()
        # Correlation id for evaluation backends: strands activates the agent
        # span around the run loop, so the ambient trace id is this
        # invocation's trace. None (and omitted) when telemetry is disabled.
        trace_id = current_trace_id()
        if trace_id:
            data["trace_id"] = trace_id
        return data

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register hook callbacks for agent and multiagent events."""
        # Agent-level
        registry.add_callback(BeforeInvocationEvent, self._on_agent_start)
        registry.add_callback(AfterModelCallEvent, self._on_model_error)
        registry.add_callback(BeforeToolCallEvent, self._on_tool_start)
        registry.add_callback(AfterToolCallEvent, self._on_tool_end)
        registry.add_callback(AfterInvocationEvent, self._on_complete)
        # Multiagent-level
        registry.add_callback(BeforeNodeCallEvent, self._on_node_start)
        registry.add_callback(AfterNodeCallEvent, self._on_node_stop)
        registry.add_callback(BeforeMultiAgentInvocationEvent, self._on_multiagent_start)
        registry.add_callback(AfterMultiAgentInvocationEvent, self._on_multiagent_complete)

    # -- Agent hooks ---------------------------------------------------------

    def _on_agent_start(self, event: BeforeInvocationEvent) -> None:
        """Emit STREAM_GROUP_START (if grouped) then AGENT_START.

        When the agent is resuming from an interrupt, the invocation is a
        continuation of the same logical run, so         the existing stream group is
        reused (no new ``#N`` group, no group-resetting STREAM_GROUP_START).
        """
        push_cost_box()
        self._errored_by_thread[get_thread_id()] = False

        istate = getattr(event.agent, "_interrupt_state", None)
        resuming = bool(istate is not None and getattr(istate, "activated", False))

        if resuming:
            self._callback(
                StreamEvent(
                    type=EventType.AGENT_START,
                    agent_name=self._agent_name,
                    data=self._enrich({}),
                ),
            )
            return

        thread_id = get_thread_id()
        count = self._invocation_count_by_thread.get(thread_id, 0) + 1
        self._invocation_count_by_thread[thread_id] = count
        if self._stream_group:
            self._active_group_by_thread[thread_id] = (
                f"{self._stream_group}#{count}" if count > 1 else self._stream_group
            )
            parent = self._stream_group.rsplit(".", 1)[0] if "." in self._stream_group else None
            # BeforeInvocationEvent fires before the task message is appended to
            # agent.messages, but it carries the incoming messages directly; fall
            # back to the agent transcript for safety.
            task = _extract_incoming_task(
                getattr(event, "messages", None) or getattr(event.agent, "messages", None)
            )
            self._callback(
                StreamEvent(
                    type=EventType.STREAM_GROUP_START,
                    agent_name=self._agent_name,
                    data=self._enrich(
                        {
                            "parent_group": parent,
                            # The tool-call id the coordinator used to delegate to this
                            # agent. Stable key for correlating the inline card in the
                            # UI with this group (see delegation_context / _activity).
                            "tool_call_id": get_delegation_id(),
                            # The task this agent was handed (its latest human/handoff
                            # message), shown on the card; and whether this agent's text
                            # IS the chat reply (so the UI suppresses only its duplicate).
                            "task": task,
                            "is_chat_reply": self._is_chat_reply,
                            "inline_chat_owner": self._inline_chat_owner,
                        }
                    ),
                ),
            )
        self._callback(
            StreamEvent(
                type=EventType.AGENT_START,
                agent_name=self._agent_name,
                data=self._enrich({}),
            ),
        )

    def _on_tool_start(self, event: BeforeToolCallEvent) -> None:
        """Register a pending tool call and emit a TOOL_START streaming event."""
        raw_name = event.tool_use.get("name", "unknown")
        tool_label = _resolve_tool_label(raw_name, self._tool_labels) or raw_name
        tool_use_id = event.tool_use.get("toolUseId", "")

        logger.debug(
            "TOOL_START agent=%s tool=%s tool_use_id=%s", self._agent_name, raw_name, tool_use_id
        )
        self._callback(
            StreamEvent(
                type=EventType.TOOL_START,
                agent_name=self._agent_name,
                data=self._enrich(
                    {
                        "tool_name": raw_name,
                        "tool_label": tool_label,
                        "tool_use_id": tool_use_id,
                        "tool_input": event.tool_use.get("input", {}),
                    }
                ),
            ),
        )

    def _on_tool_end(self, event: AfterToolCallEvent) -> None:
        """Complete a pending tool call, accumulate the step, and emit TOOL_END."""
        raw_name = event.tool_use.get("name", "unknown")
        tool_label = _resolve_tool_label(raw_name, self._tool_labels) or raw_name
        tool_use_id = event.tool_use.get("toolUseId", "")

        outcome = tool_outcome(event)
        status = outcome["status"]

        logger.debug(
            "TOOL_END agent=%s tool=%s tool_use_id=%s status=%s",
            self._agent_name,
            raw_name,
            tool_use_id,
            status,
        )
        data: dict[str, Any] = {
            "tool_name": raw_name,
            "tool_label": tool_label,
            "tool_use_id": tool_use_id,
            "status": status,
            "error": outcome.get("error") or None,
            "fix": outcome.get("fix"),
            "tool_result": _extract_result_text(event.result, self._max_result_len),
        }
        approvals = (getattr(event, "invocation_state", None) or {}).get(APPROVALS_STATE_KEY)
        if isinstance(approvals, dict) and tool_use_id in approvals:
            data["approval"] = approvals[tool_use_id]
        self._callback(
            StreamEvent(
                type=EventType.TOOL_END,
                agent_name=self._agent_name,
                data=self._enrich(data),
            ),
        )

    def _on_complete(self, event: AfterInvocationEvent) -> None:
        """Emit AGENT_COMPLETE (and STREAM_GROUP_END if grouped).

        Suppressed when the invocation errored — an ERROR event was
        already emitted via :meth:`_on_model_error`.
        """
        cost = pop_cost_box()
        if self._errored_by_thread.get(get_thread_id(), False):
            if self._stream_group:
                self._callback(
                    StreamEvent(
                        type=EventType.STREAM_GROUP_END,
                        agent_name=self._agent_name,
                        data=self._enrich({"status": "error"}),
                    ),
                )
            return

        result = event.result
        if result is not None and result.stop_reason == "interrupt":
            for interrupt in result.interrupts or []:
                self._callback(
                    StreamEvent(
                        type=EventType.INTERRUPT,
                        agent_name=self._agent_name,
                        data=self._enrich(
                            {
                                "interrupt_id": interrupt.id,
                                "name": interrupt.name,
                                "reason": interrupt.reason,
                            }
                        ),
                    ),
                )
            return

        metrics = event.agent.event_loop_metrics
        invocation = metrics.latest_agent_invocation
        usage = invocation.usage if invocation else metrics.accumulated_usage

        data: dict[str, Any] = self._enrich(
            {
                "usage": {
                    "input_tokens": usage.get("inputTokens", 0),
                    "output_tokens": usage.get("outputTokens", 0),
                    "total_tokens": usage.get("totalTokens", 0),
                    "cache_read_input_tokens": usage.get("cacheReadInputTokens", 0),
                    "cache_write_input_tokens": usage.get("cacheWriteInputTokens", 0),
                    "cost": cost,
                },
                "text": str(result) if result is not None else "",
                "message": result.message if result is not None else {},
                "model_id": (getattr(event.agent.model, "config", None) or {}).get("model_id"),
            }
        )

        if "trace_id" not in data:
            trace_id = current_trace_id(span=getattr(event.agent, "trace_span", None))
            if trace_id:
                data["trace_id"] = trace_id

        structured = getattr(result, "structured_output", None) if result is not None else None
        if structured is not None:
            try:
                data["structured_output"] = structured.model_dump()
                data["output_schema_name"] = type(structured).__name__
            except Exception:  # nosec B110
                pass

        self._callback(
            StreamEvent(
                type=EventType.AGENT_COMPLETE,
                agent_name=self._agent_name,
                data=data,
            ),
        )

        if self._stream_group:
            self._callback(
                StreamEvent(
                    type=EventType.STREAM_GROUP_END,
                    agent_name=self._agent_name,
                    data=self._enrich({"status": "completed"}),
                ),
            )

    # -- Model error hook ----------------------------------------------------

    def _on_model_error(self, event: AfterModelCallEvent) -> None:
        """Emit ERROR when a model call fails.

        Fires for provider-level exceptions such as expired credentials,
        throttling, network errors, or any other model API failure.
        Sets ``_errored`` to suppress the subsequent misleading AGENT_COMPLETE.
        """
        if event.exception is None:
            return

        self._errored_by_thread[get_thread_id()] = True
        self._callback(
            StreamEvent(
                type=EventType.ERROR,
                agent_name=self._agent_name,
                data=self._enrich(
                    {
                        "text": f"{event.exception}",
                        "exception_type": type(event.exception).__name__,
                    }
                ),
            ),
        )

    # -- Multiagent hooks ----------------------------------------------------

    def _on_multiagent_start(self, event: BeforeMultiAgentInvocationEvent) -> None:
        """Emit MULTIAGENT_START at the beginning of each multi-agent orchestration."""
        self._callback(
            StreamEvent(
                type=EventType.MULTIAGENT_START,
                agent_name=self._agent_name,
                data={
                    "multiagent_type": event.source.__class__.__name__.lower(),
                },
            ),
        )

    def _on_node_start(self, event: BeforeNodeCallEvent) -> None:
        """Emit NODE_START when a multi-agent orchestration begins a node."""
        self._callback(
            StreamEvent(
                type=EventType.NODE_START,
                agent_name=self._agent_name,
                data={
                    "node_id": event.node_id,
                    "multiagent_type": event.source.__class__.__name__.lower(),
                },
            ),
        )

    def _on_node_stop(self, event: AfterNodeCallEvent) -> None:
        """Emit NODE_STOP when a multi-agent orchestration finishes a node."""
        self._callback(
            StreamEvent(
                type=EventType.NODE_STOP,
                agent_name=self._agent_name,
                data={
                    "node_id": event.node_id,
                    "multiagent_type": event.source.__class__.__name__.lower(),
                },
            ),
        )

    def _on_multiagent_complete(self, event: AfterMultiAgentInvocationEvent) -> None:
        """Emit MULTIAGENT_COMPLETE when the orchestration finishes."""
        self._callback(
            StreamEvent(
                type=EventType.MULTIAGENT_COMPLETE,
                agent_name=self._agent_name,
                data={
                    "multiagent_type": event.source.__class__.__name__.lower(),
                },
            ),
        )

    # -- Callback handler for streaming chunks -------------------------------

    def as_callback_handler(self) -> Callable[..., None]:
        """Return a strands-compatible callback_handler for TOKEN, REASONING, and HANDOFF events.

        Handles the following kwarg patterns emitted by strands:
        - ``data`` (str): A streamed text chunk -> TOKEN event.
        - ``reasoningText`` (str): A reasoning chunk -> REASONING event.
        - ``type == "multiagent_handoff"``: A :class:`~strands.types._events.MultiAgentHandoffEvent`
          fired during Swarm/Graph node transitions -> HANDOFF event.

        Returns:
            A callable compatible with strands ``callback_handler`` interface.
        """

        def _handler(**kwargs: Any) -> None:
            text: str = kwargs.get("data", "")
            if text:
                self._callback(
                    StreamEvent(
                        type=EventType.TOKEN,
                        agent_name=self._agent_name,
                        data=self._enrich({"text": text}),
                    ),
                )

            current = kwargs.get("current_tool_use")
            if (
                kwargs.get("type") == "tool_use_stream"
                and isinstance(current, dict)
                and current.get("toolUseId")
            ):
                self._callback(
                    StreamEvent(
                        type=EventType.TOOL_INPUT,
                        agent_name=self._agent_name,
                        data=self._enrich(
                            {
                                "tool_use_id": current["toolUseId"],
                                "tool_name": current.get("name", ""),
                                "tool_label": _resolve_tool_label(
                                    current.get("name", ""), self._tool_labels
                                ),
                                "input": current.get("input", ""),
                            }
                        ),
                    ),
                )

            reasoning: str = kwargs.get("reasoningText", "")
            if reasoning:
                self._callback(
                    StreamEvent(
                        type=EventType.REASONING,
                        agent_name=self._agent_name,
                        data=self._enrich({"text": reasoning}),
                    ),
                )

            if kwargs.get("type") == "multiagent_handoff":
                self._callback(
                    StreamEvent(
                        type=EventType.HANDOFF,
                        agent_name=self._agent_name,
                        data=self._enrich(
                            {
                                "from_node_ids": kwargs.get("from_node_ids", []),
                                "to_node_ids": kwargs.get("to_node_ids", []),
                                "message": kwargs.get("message"),
                            }
                        ),
                    )
                )

        return _handler
