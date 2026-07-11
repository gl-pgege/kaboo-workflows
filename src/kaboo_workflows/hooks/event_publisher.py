"""EventPublisher hook for streaming agent activities to external consumers.

Key Features:
    - Unified single-agent and multi-agent event publishing
    - Safe callback wrapping that logs instead of propagating exceptions
    - Longest-prefix tool label resolution for display names
    - Callback handler factory for TOKEN, REASONING, and HANDOFF events
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
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
from ..types import EventType, StreamEvent

logger = logging.getLogger(__name__)

EventCallback = Callable[[StreamEvent], None]

_MAX_RESULT_LEN = 600


# ============================================================================
# Helpers
# ============================================================================


def _extract_result_text(result: Any, max_len: int = _MAX_RESULT_LEN) -> str | None:
    """Extract a plain-text summary from a strands tool result (for streaming TOOL_END)."""
    if result is None:
        return None
    parts: list[str] = []
    for block in result.get("content", []):
        if "text" in block:
            parts.append(block["text"])
        elif "json" in block:
            parts.append(str(block["json"]))
    raw = "\n".join(parts)
    if not raw:
        return None
    return raw[:max_len] + "..." if len(raw) > max_len else raw


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
        max_result_len: int = 600,
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
                in TOOL_END events. Default: 600.

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
        data["thread_id"] = thread_id
        data["run_id"] = get_run_id()
        data["turn_id"] = get_turn_id()
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

        result_status = event.result.get("status") if isinstance(event.result, dict) else None
        status = "error" if event.exception or result_status == "error" else "success"

        logger.debug(
            "TOOL_END agent=%s tool=%s tool_use_id=%s status=%s",
            self._agent_name,
            raw_name,
            tool_use_id,
            status,
        )
        self._callback(
            StreamEvent(
                type=EventType.TOOL_END,
                agent_name=self._agent_name,
                data=self._enrich(
                    {
                        "tool_name": raw_name,
                        "tool_label": tool_label,
                        "tool_use_id": tool_use_id,
                        "status": status,
                        "error": str(event.exception) if event.exception else None,
                        "tool_result": _extract_result_text(event.result, self._max_result_len),
                    }
                ),
            ),
        )

    def _on_complete(self, event: AfterInvocationEvent) -> None:
        """Emit AGENT_COMPLETE (and STREAM_GROUP_END if grouped).

        Suppressed when the invocation errored — an ERROR event was
        already emitted via :meth:`_on_model_error`.
        """
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
                },
                "text": str(result) if result is not None else "",
                "message": result.message if result is not None else {},
            }
        )

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
