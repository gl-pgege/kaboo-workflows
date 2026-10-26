"""Sub-agents on the wire as standard AG-UI 1.0 events.

Every agent a delegate runs reports to its :class:`~kaboo_workflows.hooks.EventPublisher`.
:class:`SubagentStream` turns those reports into the protocol's own events: a
``SUBAGENT_STARTED`` when an invocation's segment of the run begins, its text,
reasoning and tool calls tagged with the invocation's ``subagentRunId`` (tool
arguments streamed as the model writes them), and a ``SUBAGENT_FINISHED``,
completed or suspended, or a ``SUBAGENT_ERROR`` when it ends.

An invocation's ``subagentRunId`` is its stream group (``orchestrator.analyze``,
``orchestrator.analyze#2`` for a second call). A paused invocation keeps it, so
the run that resumes it opens a new segment of the same invocation.

:class:`RunEvents` puts it all together for one response.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from ag_ui.core import (
    BaseEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunAgentInput,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    StateDeltaEvent,
    SubagentErrorEvent,
    SubagentFinishedEvent,
    SubagentStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    TokenUsage,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.core import EventType as AGUIEventType

from ..tools._browser import browser_wait_call
from ..types import EventType, StreamEvent
from ._interrupts import kaboo_reason, lift_interrupt
from ._order import AppendOnly

_OPENING = frozenset(
    {
        EventType.AGENT_START,
        EventType.TOKEN,
        EventType.REASONING,
        EventType.TOOL_INPUT,
        EventType.TOOL_START,
        EventType.TOOL_END,
    }
)


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class _Invocation:
    run_id: str
    name: str
    parent_tool_call_id: str | None = None
    parent_run_id: str | None = None
    anchor: str | None = None
    title: str | None = None
    task: str | None = None
    open: bool = False
    text_id: str | None = None
    text: str = ""
    reasoning_id: str | None = None
    step_id: str | None = None
    tool_args: dict[str, str] = field(default_factory=dict)
    ended_tools: set[str] = field(default_factory=set)


class SubagentStream:
    """Translates publisher events from delegated agents into AG-UI events.

    ``carried`` are the sub-agent messages the client sent with the run. A call
    they hold is never announced again: a resumed invocation reports only what
    is new, its result.
    """

    def __init__(self, carried: list[Any] | None = None) -> None:
        self._invocations: dict[str, _Invocation] = {}
        self._owners: dict[str, str] = {}
        self._known_calls = {
            call.id
            for message in carried or []
            for call in getattr(message, "tool_calls", None) or []
        }
        self.usage: list[TokenUsage] = []
        self.trace_id: str | None = None

    # -- Translation ---------------------------------------------------------

    def translate(self, event: StreamEvent) -> list[BaseEvent]:
        """Return the AG-UI events one publisher event amounts to."""
        self.trace_id = self.trace_id or event.data.get("trace_id") or None
        group = event.data.get("stream_group")
        if not isinstance(group, str) or not group or event.data.get("inline_chat_owner"):
            return []
        if event.type == EventType.STREAM_GROUP_START:
            return self._begin(group, event)
        handlers = {
            EventType.AGENT_START: self._resume,
            EventType.TOKEN: self._token,
            EventType.REASONING: self._reasoning,
            EventType.TOOL_INPUT: self._tool_input,
            EventType.TOOL_START: self._tool_start,
            EventType.TOOL_END: self._tool_end,
            EventType.INTERRUPT: self._interrupt,
            EventType.AGENT_COMPLETE: self._complete,
            EventType.ERROR: self._error,
        }
        handler = handlers.get(event.type)
        if handler is None:
            return []
        invocation = self._invocations.get(group)
        out: list[BaseEvent] = []
        if invocation is None or not invocation.open:
            # Only new work opens a segment: an invocation resuming in this run.
            if event.type not in _OPENING:
                return []
            out.extend(self._begin(group, event))
            invocation = self._invocations[group]
        out.extend(handler(invocation, event))
        return out

    def _resume(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        del invocation, event
        return []

    def _begin(self, group: str, event: StreamEvent) -> list[BaseEvent]:
        invocation = self._invocations.get(group)
        if invocation is None:
            parent_call = event.data.get("tool_call_id")
            parent_call = parent_call if isinstance(parent_call, str) and parent_call else None
            parent_run = self._owners.get(parent_call) if parent_call else None
            parent = self._invocations.get(parent_run) if parent_run else None
            invocation = _Invocation(
                run_id=group,
                name=event.agent_name,
                parent_tool_call_id=parent_call,
                parent_run_id=parent_run,
                anchor=parent.anchor if parent else parent_call,
                title=event.data.get("stream_title") or None,
                task=event.data.get("task") or None,
            )
            self._invocations[group] = invocation
        if invocation.open:
            return []
        invocation.open = True
        metadata = {
            key: value
            for key, value in (
                ("title", invocation.title),
                ("task", invocation.task),
                ("traceId", event.data.get("trace_id")),
            )
            if value
        }
        return [
            SubagentStartedEvent(
                type=AGUIEventType.SUBAGENT_STARTED,
                subagent_run_id=invocation.run_id,
                name=invocation.name,
                parent_tool_call_id=invocation.parent_tool_call_id,
                parent_subagent_run_id=(
                    invocation.parent_run_id
                    if invocation.parent_run_id in self._invocations
                    and self._invocations[invocation.parent_run_id].open
                    else None
                ),
                metadata=metadata or None,
            )
        ]

    def _token(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        text = event.data.get("text") or ""
        if not text:
            return []
        out = self._close_reasoning(invocation)
        if invocation.text_id is None:
            invocation.text_id = _new_id()
            invocation.step_id = invocation.text_id
            invocation.text = ""
            out.append(
                TextMessageStartEvent(
                    type=AGUIEventType.TEXT_MESSAGE_START,
                    message_id=invocation.text_id,
                    role="assistant",
                    subagent_run_id=invocation.run_id,
                )
            )
        invocation.text += text
        out.append(
            TextMessageContentEvent(
                type=AGUIEventType.TEXT_MESSAGE_CONTENT,
                message_id=invocation.text_id,
                delta=text,
                subagent_run_id=invocation.run_id,
            )
        )
        return out

    def _reasoning(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        text = event.data.get("text") or ""
        if not text:
            return []
        out = self._close_text(invocation)
        if invocation.reasoning_id is None:
            invocation.reasoning_id = _new_id()
            invocation.step_id = None
            out.append(
                ReasoningStartEvent(
                    type=AGUIEventType.REASONING_START,
                    message_id=invocation.reasoning_id,
                    subagent_run_id=invocation.run_id,
                )
            )
            out.append(
                ReasoningMessageStartEvent(
                    type=AGUIEventType.REASONING_MESSAGE_START,
                    message_id=invocation.reasoning_id,
                    role="reasoning",
                    subagent_run_id=invocation.run_id,
                )
            )
        out.append(
            ReasoningMessageContentEvent(
                type=AGUIEventType.REASONING_MESSAGE_CONTENT,
                message_id=invocation.reasoning_id,
                delta=text,
                subagent_run_id=invocation.run_id,
            )
        )
        return out

    def _start_call(
        self, invocation: _Invocation, tool_use_id: str, event: StreamEvent
    ) -> list[BaseEvent]:
        out = [*self._close_reasoning(invocation), *self._close_text(invocation)]
        if invocation.step_id is None:
            invocation.step_id = _new_id()
        invocation.tool_args[tool_use_id] = ""
        self._owners[tool_use_id] = invocation.run_id
        name = event.data.get("tool_name") or ""
        label = event.data.get("tool_label")
        out.append(
            ToolCallStartEvent(
                type=AGUIEventType.TOOL_CALL_START,
                tool_call_id=tool_use_id,
                tool_call_name=name,
                parent_message_id=invocation.step_id,
                subagent_run_id=invocation.run_id,
                metadata={"label": label} if label and label != name else None,
            )
        )
        return out

    def _args(self, invocation: _Invocation, tool_use_id: str, arguments: str) -> list[BaseEvent]:
        sent = invocation.tool_args.get(tool_use_id, "")
        if not arguments.startswith(sent) or arguments == sent:
            return []
        invocation.tool_args[tool_use_id] = arguments
        return [
            ToolCallArgsEvent(
                type=AGUIEventType.TOOL_CALL_ARGS,
                tool_call_id=tool_use_id,
                delta=arguments[len(sent) :],
                subagent_run_id=invocation.run_id,
            )
        ]

    def _tool_input(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        tool_use_id = event.data.get("tool_use_id")
        if (
            not tool_use_id
            or tool_use_id in invocation.ended_tools
            or tool_use_id in self._known_calls
        ):
            return []
        out: list[BaseEvent] = []
        if tool_use_id not in invocation.tool_args:
            out.extend(self._start_call(invocation, tool_use_id, event))
        raw = event.data.get("input")
        out.extend(self._args(invocation, tool_use_id, raw if isinstance(raw, str) else ""))
        return out

    def _tool_start(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        tool_use_id = event.data.get("tool_use_id")
        if not tool_use_id or tool_use_id in invocation.ended_tools:
            return []
        if tool_use_id in self._known_calls:
            invocation.ended_tools.add(tool_use_id)
            self._owners[tool_use_id] = invocation.run_id
            return []
        out: list[BaseEvent] = []
        if tool_use_id not in invocation.tool_args:
            out.extend(self._start_call(invocation, tool_use_id, event))
        tool_input = event.data.get("tool_input")
        arguments = json.dumps(tool_input if tool_input is not None else {})
        if invocation.tool_args.get(tool_use_id, "") == "":
            out.extend(self._args(invocation, tool_use_id, arguments))
        invocation.ended_tools.add(tool_use_id)
        out.append(
            ToolCallEndEvent(
                type=AGUIEventType.TOOL_CALL_END,
                tool_call_id=tool_use_id,
                subagent_run_id=invocation.run_id,
            )
        )
        return out

    def _tool_end(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        tool_use_id = event.data.get("tool_use_id")
        if not tool_use_id:
            return []
        out: list[BaseEvent] = []
        if tool_use_id not in invocation.ended_tools:
            out.extend(self._tool_start(invocation, event))
        result = event.data.get("tool_result")
        content = (
            result if isinstance(result, str) else json.dumps(result) if result is not None else ""
        )
        message_id = _new_id()
        invocation.step_id = None
        label = event.data.get("tool_label")
        approval = event.data.get("approval")
        failed = event.data.get("status") == "error"
        metadata = {
            key: value
            for key, value in (
                ("label", label if label != event.data.get("tool_name") else None),
                ("approval", approval if isinstance(approval, dict) else None),
                ("status", "error" if failed else "success"),
                ("error", event.data.get("error") if failed else None),
                ("fix", event.data.get("fix") if failed else None),
            )
            if value
        }
        out.append(
            ToolCallResultEvent(
                type=AGUIEventType.TOOL_CALL_RESULT,
                message_id=message_id,
                tool_call_id=tool_use_id,
                content=content,
                role="tool",
                subagent_run_id=invocation.run_id,
                metadata=metadata,
            )
        )
        return out

    def _interrupt(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        interrupt_id = event.data.get("interrupt_id")
        return self._end(invocation, suspended=[interrupt_id] if interrupt_id else [])

    def _complete(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        usage = event.data.get("usage")
        metadata: dict[str, Any] = {}
        if isinstance(usage, dict):
            token_usage = TokenUsage(
                model=event.data.get("model_id") or None,
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                total_tokens=usage.get("total_tokens") or 0,
                cached_input_tokens=usage.get("cache_read_input_tokens") or None,
                cache_write_input_tokens=usage.get("cache_write_input_tokens") or None,
            )
            self.usage.append(token_usage)
            metadata["usage"] = token_usage.model_dump(by_alias=True, exclude_none=True)
            if usage.get("cost") is not None:
                metadata["cost"] = usage["cost"]
        structured = event.data.get("structured_output")
        if structured is not None and event.data.get("output_schema_name"):
            metadata["outputSchema"] = event.data["output_schema_name"]
        result = structured if structured is not None else (event.data.get("text") or None)
        return self._end(invocation, result=result, metadata=metadata or None)

    def _error(self, invocation: _Invocation, event: StreamEvent) -> list[BaseEvent]:
        out = [*self._close_reasoning(invocation), *self._close_text(invocation)]
        invocation.open = False
        out.append(
            SubagentErrorEvent(
                type=AGUIEventType.SUBAGENT_ERROR,
                subagent_run_id=invocation.run_id,
                message=str(event.data.get("text") or "The agent failed."),
                code=event.data.get("exception_type") or None,
            )
        )
        return out

    def _end(
        self,
        invocation: _Invocation,
        *,
        result: Any = None,
        suspended: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[BaseEvent]:
        if not invocation.open:
            return []
        out = [*self._close_reasoning(invocation), *self._close_text(invocation)]
        invocation.open = False
        outcome: dict[str, Any] = (
            {"type": "suspended", "interruptIds": suspended}
            if suspended is not None
            else {"type": "success"}
        )
        out.append(
            SubagentFinishedEvent.model_validate(
                {
                    "type": AGUIEventType.SUBAGENT_FINISHED,
                    "subagentRunId": invocation.run_id,
                    "result": result,
                    "outcome": outcome,
                    **({"metadata": metadata} if metadata else {}),
                }
            )
        )
        return out

    def _close_text(self, invocation: _Invocation) -> list[BaseEvent]:
        if invocation.text_id is None:
            return []
        message_id = invocation.text_id
        invocation.text_id = None
        return [
            TextMessageEndEvent(
                type=AGUIEventType.TEXT_MESSAGE_END,
                message_id=message_id,
                subagent_run_id=invocation.run_id,
            )
        ]

    def _close_reasoning(self, invocation: _Invocation) -> list[BaseEvent]:
        if invocation.reasoning_id is None:
            return []
        message_id = invocation.reasoning_id
        invocation.reasoning_id = None
        return [
            ReasoningMessageEndEvent(
                type=AGUIEventType.REASONING_MESSAGE_END,
                message_id=message_id,
                subagent_run_id=invocation.run_id,
            ),
            ReasoningEndEvent(
                type=AGUIEventType.REASONING_END,
                message_id=message_id,
                subagent_run_id=invocation.run_id,
            ),
        ]

    def close(self, *, suspended: bool) -> list[BaseEvent]:
        """End every segment still open, innermost first, before the run ends."""
        out: list[BaseEvent] = []
        for invocation in reversed(list(self._invocations.values())):
            if invocation.open:
                for tool_use_id in list(invocation.tool_args):
                    if tool_use_id not in invocation.ended_tools:
                        invocation.ended_tools.add(tool_use_id)
                        out.append(
                            ToolCallEndEvent(
                                type=AGUIEventType.TOOL_CALL_END,
                                tool_call_id=tool_use_id,
                                subagent_run_id=invocation.run_id,
                            )
                        )
                out.extend(self._end(invocation, suspended=[] if suspended else None))
        return out


def split_subagent_messages(input_data: RunAgentInput) -> list[Any]:
    """Take the sub-agent messages out of a run's input.

    They are the client's copy of what sub-agents said, kept for display: the
    entry agent must not read them as its own conversation.
    """
    kept: list[Any] = []
    carried: list[Any] = []
    for message in input_data.messages or []:
        (carried if getattr(message, "subagent_run_id", None) else kept).append(message)
    if carried:
        input_data.messages = kept
    return carried


def merge_usage(usage: list[TokenUsage] | None, extra: list[TokenUsage]) -> list[TokenUsage] | None:
    """Add sub-agents' token counts to a run's, one entry per provider and model."""
    if not extra:
        return usage
    merged: dict[tuple[str | None, str | None], TokenUsage] = {}
    for entry in [*(usage or []), *extra]:
        key = (entry.provider, entry.model)
        current = merged.get(key)
        if current is None:
            merged[key] = entry.model_copy()
            continue
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
        ):
            a, b = getattr(current, name), getattr(entry, name)
            if a is not None or b is not None:
                setattr(current, name, (a or 0) + (b or 0))
    return list(merged.values())


class RunEvents:
    """Everything one response adds to the events its run produces.

    Publisher events go through :meth:`sink` (the event queue's sink) and come
    out as sub-agent events on the response queue, in the order they happened.
    Each event leaving the queue goes through :meth:`emit`, which, at the end,
    closes the sub-agents still open and adds their token counts to the run's.
    :meth:`emit` also lets each agent extend only its newest message, so a
    client that appends events in arrival order draws the run in the order it
    happened.
    """

    def __init__(
        self,
        merged: Any,
        carried: list[Any],
        *,
        subagents: bool = True,
        browser_tools: Collection[str] = (),
    ) -> None:
        self._merged = merged
        self._order = AppendOnly()
        self.stream = SubagentStream(carried) if subagents else None
        self._browser_tools = frozenset(browser_tools)
        self._browser_calls: dict[str, None] = {}
        self._call_runs: dict[str, str] = {
            call.id: run
            for message in carried
            if (run := getattr(message, "subagent_run_id", None))
            for call in getattr(message, "tool_calls", None) or []
        }

    def sink(self, event: Any) -> None:
        if self.stream is None or not isinstance(event, StreamEvent):
            return
        for translated in self.stream.translate(event):
            self._merged.put_nowait(translated)

    def delta(self, operations: list[dict[str, Any]]) -> None:
        """Send state operations to the client as a ``STATE_DELTA``, in order with the run."""
        self._merged.put_nowait(StateDeltaEvent(type=AGUIEventType.STATE_DELTA, delta=operations))

    def emit(self, item: Any) -> list[Any]:
        return [self._order.place(event) for event in self._emit(item)]

    def _emit(self, item: Any) -> list[Any]:
        etype = getattr(item, "type", None)
        stream = self.stream
        if etype == AGUIEventType.TOOL_CALL_START:
            if item.tool_call_name in self._browser_tools:
                self._browser_calls[item.tool_call_id] = None
            if run := getattr(item, "subagent_run_id", None):
                self._call_runs[item.tool_call_id] = run
        elif etype == AGUIEventType.TOOL_CALL_RESULT:
            self._browser_calls.pop(item.tool_call_id, None)
        if etype == AGUIEventType.RUN_FINISHED and isinstance(item, RunFinishedEvent):
            outcome = getattr(item, "outcome", None)
            if isinstance(outcome, RunFinishedInterruptOutcome):
                # A delegated browser tool's wait is a pending call, not an interrupt.
                kept = [
                    lift_interrupt(i, self._call_runs.get)
                    for i in outcome.interrupts
                    if not _is_browser_wait(i)
                ]
                outcome = (
                    outcome.model_copy(update={"interrupts": kept})
                    if kept
                    else RunFinishedSuccessOutcome(type="success")
                )
                item = item.model_copy(update={"outcome": outcome})
            waiting = list(self._browser_calls)
            suspended = getattr(outcome, "type", None) == "interrupt" or bool(waiting)
            closing = stream.close(suspended=suspended) if stream else []
            update: dict[str, Any] = {}
            if stream and (usage := merge_usage(getattr(item, "usage", None), stream.usage)):
                update["usage"] = usage
            if stream and stream.trace_id:
                update["metadata"] = {**(item.metadata or {}), "traceId": stream.trace_id}
            if waiting and getattr(outcome, "type", "success") == "success":
                update["outcome"] = RunFinishedSuccessOutcome(
                    type="success", pending_tool_call_ids=waiting
                )
            return [*closing, item.model_copy(update=update) if update else item]
        return [item]


def _is_browser_wait(interrupt: Any) -> bool:
    return browser_wait_call(kaboo_reason(interrupt)) is not None


def fold_subagents(events: list[Any]) -> dict[str, dict[str, Any]]:
    """Fold sub-agent events into one group per invocation, the way a UI holds them.

    The reference for what a client builds from the standard events: each group
    has the invocation's agent, the call that started it, its parent, its
    status, tool calls, token usage and the runs it spanned.
    """
    groups: dict[str, dict[str, Any]] = {}
    tools: dict[str, dict[str, Any]] = {}
    current_run: str | None = None
    for event in events:
        etype = getattr(event, "type", None)
        run_id = getattr(event, "subagent_run_id", None)
        if etype == AGUIEventType.RUN_STARTED:
            current_run = event.run_id
        elif etype == AGUIEventType.SUBAGENT_STARTED:
            group = groups.setdefault(
                event.subagent_run_id,
                {
                    "agentName": event.name,
                    "toolCallId": event.parent_tool_call_id,
                    "parentGroup": event.parent_subagent_run_id,
                    "title": (event.metadata or {}).get("title"),
                    "task": (event.metadata or {}).get("task"),
                    "traceId": (event.metadata or {}).get("traceId"),
                    "tools": [],
                    "text": "",
                    "usage": None,
                    "runId": current_run,
                    "runIds": [],
                },
            )
            group["status"] = "active"
            if current_run is not None and current_run not in group["runIds"]:
                group["runIds"].append(current_run)
        elif run_id not in groups:
            continue
        elif etype == AGUIEventType.TEXT_MESSAGE_CONTENT:
            groups[run_id]["text"] += event.delta
        elif etype == AGUIEventType.TOOL_CALL_START:
            if event.tool_call_id in tools:
                tools[event.tool_call_id].update(status="running", args="")
                continue
            tool = {
                "toolUseId": event.tool_call_id,
                "toolName": event.tool_call_name,
                "status": "running",
                "args": "",
            }
            if label := (event.metadata or {}).get("label"):
                tool["toolLabel"] = label
            tools[event.tool_call_id] = tool
            groups[run_id]["tools"].append(tool)
        elif etype == AGUIEventType.TOOL_CALL_ARGS and event.tool_call_id in tools:
            tools[event.tool_call_id]["args"] += event.delta
        elif etype == AGUIEventType.TOOL_CALL_RESULT and event.tool_call_id in tools:
            tools[event.tool_call_id].update(status="done", result=event.content)
            approval = (event.metadata or {}).get("approval")
            if approval:
                tools[event.tool_call_id]["approval"] = approval
        elif etype == AGUIEventType.SUBAGENT_FINISHED:
            outcome = getattr(event.outcome, "type", "success") if event.outcome else "success"
            groups[run_id]["status"] = "suspended" if outcome == "suspended" else "completed"
            groups[run_id]["result"] = event.result
            usage = (event.metadata or {}).get("usage")
            if usage:
                groups[run_id]["usage"] = usage
            schema = (event.metadata or {}).get("outputSchema")
            if schema:
                groups[run_id]["outputSchemaName"] = schema
        elif etype == AGUIEventType.SUBAGENT_ERROR:
            groups[run_id]["status"] = "error"
    return groups
