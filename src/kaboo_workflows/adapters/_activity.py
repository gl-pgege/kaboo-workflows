"""Per-conversation activity state builder.

Folds kaboo ``StreamEvent``s into a hierarchical activity snapshot
(``{groups: {...}}``) keyed by ``thread_id``. The snapshot is emitted onto the
AG-UI run stream as ``ACTIVITY_SNAPSHOT`` events (see ``agui.py``), so there is
no separate SSE endpoint or subscriber machinery here — this is state only.
"""

from __future__ import annotations

import copy
import logging
import threading
from typing import Any

from kaboo_workflows.types import EventType, StreamEvent

logger = logging.getLogger(__name__)


_USAGE_KEYS = (
    ("inputTokens", "input_tokens", int),
    ("outputTokens", "output_tokens", int),
    ("totalTokens", "total_tokens", int),
    ("cacheReadInputTokens", "cache_read_input_tokens", int),
    ("cacheWriteInputTokens", "cache_write_input_tokens", int),
    ("cost", "cost", float),
)


def _accumulate_usage(target: dict[str, Any], key: str, usage: dict[str, Any]) -> None:
    """Add *usage* (snake_case wire fields) into ``target[key]`` (camelCase).

    Accumulates rather than overwrites: an agent can complete multiple times
    within one turn (interrupt resumes, delegate re-entry), and each completion
    reports the tokens of that invocation only.
    """
    bucket = target.setdefault(key, {camel: cast(0) for camel, _, cast in _USAGE_KEYS})
    for camel, snake, cast in _USAGE_KEYS:
        bucket[camel] = bucket.get(camel, cast(0)) + cast(usage.get(snake, 0) or 0)


def _set_cache_hit_rate(bucket: dict[str, Any]) -> None:
    """Derive ``cacheHitRate`` = cacheRead / (input + cacheRead + cacheWrite) for a run that used the cache.

    Bedrock counts uncached input, cache reads and cache writes separately, so
    their sum is the whole prompt and the rate is the share of it read from the
    cache. A run that neither read nor wrote the prompt cache gets no rate,
    rather than a zero that would read as a cache that kept missing.
    """
    cache_read = bucket.get("cacheReadInputTokens", 0)
    cache_write = bucket.get("cacheWriteInputTokens", 0)
    prompt = bucket.get("inputTokens", 0) + cache_read + cache_write
    if prompt and (cache_read or cache_write):
        bucket["cacheHitRate"] = round(cache_read / prompt, 4)


def _fold_run_usage(state: dict[str, Any], event: StreamEvent) -> bool:
    """Fold an ``AGENT_COMPLETE`` event's usage into the per-run rollup.

    Kept independent of groups so run totals never miss an invocation —
    including the entry/manager agent, whose events are otherwise dropped from
    group rendering. The rollup lives on the state (keyed by run id) and rides
    every subsequent snapshot.
    """
    if event.type != EventType.AGENT_COMPLETE:
        return False
    usage = event.data.get("usage")
    run_id = event.data.get("run_id")
    if not isinstance(usage, dict) or not run_id:
        return False
    runs = state.setdefault("usageByRun", {})
    _accumulate_usage(runs, str(run_id), usage)
    _set_cache_hit_rate(runs[str(run_id)])
    trace_id = event.data.get("trace_id")
    if trace_id:
        # First trace id wins: nested agents share the entry invocation's
        # trace, and the run-level correlation should point at its root.
        state.setdefault("traceByRun", {}).setdefault(str(run_id), trace_id)
    return True


def _archive_group(groups: dict[str, Any], group: str, tool_call_id: str) -> None:
    archived = f"{group}@{tool_call_id}"

    def rename(path: str) -> str:
        if path == group or path.startswith(f"{group}."):
            return archived + path[len(group) :]
        return path

    entries = list(groups.items())
    groups.clear()
    for key, value in entries:
        if value.get("parentGroup"):
            value["parentGroup"] = rename(value["parentGroup"])
        groups[rename(key)] = value


def _note_run(group: dict[str, Any], run_id: str | None) -> None:
    """Add a run that resumed *group* to its ``runIds``.

    A turn paused for an approval or a question resumes in a new run, which
    reopens the turn's groups; ``runId`` stays the run that opened the group.
    """
    if not run_id:
        return
    runs = group.setdefault("runIds", [group["runId"]] if group.get("runId") else [])
    if run_id not in runs:
        runs.append(run_id)


def _update_activity_state(state: dict[str, Any], event: StreamEvent) -> bool:
    """Mutate *state* based on an incoming kaboo StreamEvent.

    Maintains a ``timeline`` list that preserves the chronological order of
    text segments and tool calls as they arrive. Adjacent TOKEN events are
    coalesced into a single text entry. Tool entries carry only a ``toolUseId``;
    the tool itself lives once in the group's ``tools`` list, which the client
    indexes by that id. The timeline is the only copy of a group's text: a flat
    ``tokens`` duplicate of it used to ride along, which no consumer read and
    which cost its full length in every snapshot and every delta.

    Returns:
        ``True`` if the state changed (and a fresh snapshot should be emitted),
        ``False`` for events that carry no stream group (nothing to render).
    """
    group = event.data.get("stream_group", "")

    usage_changed = _fold_run_usage(state, event)

    if not group:
        return usage_changed

    groups = state["groups"]

    if event.type == EventType.STREAM_GROUP_START:
        existing = groups.get(group)
        tool_call_id = event.data.get("tool_call_id")
        previous_call_id = existing.get("toolCallId") if existing is not None else None
        if previous_call_id and tool_call_id and previous_call_id != tool_call_id:
            _archive_group(groups, group, previous_call_id)
            existing = None
        run_id = event.data.get("run_id")
        if existing is not None:
            existing["status"] = "active"
            existing.pop("interrupt", None)
            _note_run(existing, run_id)
        else:
            groups[group] = {
                "title": event.data.get("stream_title", ""),
                "agentName": event.data.get("agent_name", ""),
                "parentGroup": event.data.get("parent_group"),
                "toolCallId": event.data.get("tool_call_id"),
                "runId": run_id,
                "runIds": [run_id] if run_id else [],
                "turnId": event.data.get("turn_id"),
                "traceId": event.data.get("trace_id"),
                "task": event.data.get("task"),
                "isChatReply": bool(event.data.get("is_chat_reply", False)),
                "inlineChatOwner": bool(event.data.get("inline_chat_owner", False)),
                "status": "active",
                "tools": [],
                "timeline": [],
            }
            logger.info(
                "activity.STREAM_GROUP_START group=%s agent=%s parent=%s "
                "tool_call_id=%s run_id=%s is_chat_reply=%s task=%r thread_id=%s",
                group,
                event.data.get("agent_name"),
                event.data.get("parent_group"),
                event.data.get("tool_call_id"),
                event.data.get("run_id"),
                event.data.get("is_chat_reply"),
                event.data.get("task"),
                event.data.get("thread_id"),
            )
    elif event.type == EventType.AGENT_START:
        if group in groups:
            groups[group]["status"] = "active"
            groups[group].pop("interrupt", None)
            _note_run(groups[group], event.data.get("run_id"))
    elif event.type == EventType.STREAM_GROUP_END:
        if group in groups:
            groups[group]["status"] = event.data.get("status", "completed")
    elif event.type == EventType.TOOL_START:
        if group in groups:
            tool_use_id = event.data.get("tool_use_id", "")
            existing_tool = next(
                (t for t in groups[group]["tools"] if t["toolUseId"] == tool_use_id),
                None,
            )
            if existing_tool is not None:
                existing_tool["status"] = "running"
            else:
                tool_entry = {
                    "toolUseId": tool_use_id,
                    "toolName": event.data.get("tool_name", ""),
                    "toolLabel": event.data.get("tool_label", ""),
                    "toolInput": event.data.get("tool_input"),
                    "status": "running",
                }
                groups[group]["tools"].append(tool_entry)
                # By reference, not by value: ``tools`` owns the entry. Appending
                # the object itself is free in memory but JSON has no references,
                # so it serialised every input and result twice in every snapshot.
                groups[group]["timeline"].append({"type": "tool", "toolUseId": tool_use_id})
    elif event.type == EventType.TOOL_END:
        if group in groups:
            tool_use_id = event.data.get("tool_use_id")
            status = event.data.get("status", "done")
            result = event.data.get("tool_result", "")
            for tool in groups[group]["tools"]:
                if tool["toolUseId"] == tool_use_id:
                    tool["status"] = status
                    tool["toolResult"] = result
                    approval = event.data.get("approval")
                    if isinstance(approval, dict):
                        tool["approval"] = approval
                    break
    elif event.type == EventType.TOKEN:
        if group in groups:
            text = event.data.get("text", "")
            timeline = groups[group]["timeline"]
            if timeline and timeline[-1]["type"] == "text":
                timeline[-1]["text"] += text
            else:
                timeline.append({"type": "text", "text": text})
    elif event.type == EventType.AGENT_COMPLETE:
        if group in groups:
            usage = event.data.get("usage")
            if isinstance(usage, dict):
                _accumulate_usage(groups[group], "usage", usage)
            if event.data.get("trace_id"):
                groups[group]["traceId"] = event.data["trace_id"]
            if "structured_output" in event.data:
                groups[group]["structuredOutput"] = event.data["structured_output"]
                groups[group]["outputSchemaName"] = event.data.get("output_schema_name")
    elif event.type == EventType.INTERRUPT:
        if group in groups:
            groups[group]["status"] = "interrupted"
            groups[group]["interrupt"] = {
                "id": event.data.get("interrupt_id"),
                "name": event.data.get("name"),
                "reason": event.data.get("reason"),
            }
    elif event.type == EventType.ERROR:
        if group in groups:
            groups[group]["status"] = "error"

    return True


class ActivityRegistry:
    """Thread-scoped builder of hierarchical activity state.

    One instance is created per :func:`create_agui_app` and captured in the
    endpoint closures. All mutation is guarded by a lock so concurrent request
    tasks stay consistent. Snapshots are read via :meth:`snapshot` and emitted
    on the run stream as ``ACTIVITY_SNAPSHOT`` events.
    """

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _state_for(self, thread_id: str) -> dict[str, Any]:
        return self._states.setdefault(thread_id, {"groups": {}})

    def apply(self, thread_id: str, event: StreamEvent) -> bool:
        """Fold *event* into *thread_id*'s state.

        Returns ``True`` when the state changed (caller should emit a fresh
        snapshot), ``False`` for events that carry no stream group.
        """
        with self._lock:
            state = self._state_for(thread_id)
            return _update_activity_state(state, event)

    def skip_waiting(self, thread_id: str) -> bool:
        """Close what *thread_id* was waiting on, because the person moved on.

        A message sent while a request waits starts a new turn; the groups that
        paused on it never resume. Their waiting tools are marked ``skipped``
        (with the decision, as the gate records it) and the groups completed, so
        no card keeps asking. Returns ``True`` when anything changed.
        """
        with self._lock:
            state = self._states.get(thread_id)
            if state is None:
                return False
            changed = False
            for group in state["groups"].values():
                if group.get("status") != "interrupted":
                    continue
                group["status"] = "completed"
                group.pop("interrupt", None)
                for tool in group["tools"]:
                    if tool.get("status") == "running":
                        tool["status"] = "skipped"
                        tool["approval"] = {"decision": "skipped", "by": "person"}
                changed = True
            return changed

    def apply_usage(self, thread_id: str, event: StreamEvent) -> bool:
        """Fold only an ``AGENT_COMPLETE`` event's token usage into the per-run rollup.

        Used for events excluded from group rendering (the entry/manager
        agent's own stream), whose usage must still count toward the run total.
        Returns ``True`` when usage was recorded.
        """
        with self._lock:
            state = self._state_for(thread_id)
            return _fold_run_usage(state, event)

    def snapshot(self, thread_id: str) -> dict[str, Any]:
        """Return a deep copy of the activity state for *thread_id* (``{groups: {...}}``).

        Deep-copied because the returned snapshot is encoded later on the SSE
        loop, after the lock is released, while subsequent events may still be
        mutating the live state.
        """
        with self._lock:
            state = self._states.get(thread_id)
            if state is None:
                return {"groups": {}}
            return copy.deepcopy(state)
