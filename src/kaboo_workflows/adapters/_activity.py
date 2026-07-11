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


def _update_activity_state(state: dict[str, Any], event: StreamEvent) -> bool:
    """Mutate *state* based on an incoming kaboo StreamEvent.

    Maintains a ``timeline`` list that preserves the chronological order of
    text segments and tool calls as they arrive. Adjacent TOKEN events are
    coalesced into a single text entry.

    Returns:
        ``True`` if the state changed (and a fresh snapshot should be emitted),
        ``False`` for events that carry no stream group (nothing to render).
    """
    group = event.data.get("stream_group", "")
    if not group:
        return False

    groups = state["groups"]

    if event.type == EventType.STREAM_GROUP_START:
        existing = groups.get(group)
        if existing is not None:
            existing["status"] = "active"
            existing.pop("interrupt", None)
        else:
            groups[group] = {
                "title": event.data.get("stream_title", ""),
                "agentName": event.data.get("agent_name", ""),
                "parentGroup": event.data.get("parent_group"),
                "toolCallId": event.data.get("tool_call_id"),
                "runId": event.data.get("run_id"),
                "turnId": event.data.get("turn_id"),
                "task": event.data.get("task"),
                "isChatReply": bool(event.data.get("is_chat_reply", False)),
                "inlineChatOwner": bool(event.data.get("inline_chat_owner", False)),
                "status": "active",
                "tools": [],
                "tokens": "",
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
                groups[group]["timeline"].append({"type": "tool", "tool": tool_entry})
    elif event.type == EventType.TOOL_END:
        if group in groups:
            tool_use_id = event.data.get("tool_use_id")
            status = event.data.get("status", "done")
            result = event.data.get("tool_result", "")
            for tool in groups[group]["tools"]:
                if tool["toolUseId"] == tool_use_id:
                    tool["status"] = status
                    tool["toolResult"] = result
                    break
    elif event.type == EventType.TOKEN:
        if group in groups:
            text = event.data.get("text", "")
            groups[group]["tokens"] += text
            timeline = groups[group]["timeline"]
            if timeline and timeline[-1]["type"] == "text":
                timeline[-1]["text"] += text
            else:
                timeline.append({"type": "text", "text": text})
    elif event.type == EventType.AGENT_COMPLETE:
        if group in groups and "structured_output" in event.data:
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
