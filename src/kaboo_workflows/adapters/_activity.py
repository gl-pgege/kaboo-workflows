"""Per-conversation activity registry for the ``/activity-stream`` endpoint.

Replaces the previous process-global ``_activity_state`` / ``_activity_subscribers``
so that concurrent conversations in a single self-hosted process no longer
clobber one another. State and subscribers are keyed by ``thread_id``.

A subscriber may also register with ``thread_id=None`` to receive a merged view
of every conversation — this is the backward-compatible single-tenant mode used
when a client connects to ``/activity-stream`` without a ``threadId`` query
parameter.
"""

from __future__ import annotations

import asyncio
import json
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
        ``True`` if the state changed (and subscribers should be notified),
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
    """Thread-scoped store of hierarchical activity + SSE subscribers.

    One instance is created per :func:`create_agui_app` and captured in the
    endpoint closures. All mutation is guarded by a lock so concurrent request
    tasks and the ``/activity-stream`` generators stay consistent.
    """

    _ALL = None

    def __init__(self) -> None:
        self._states: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str | None, list[asyncio.Queue[str]]] = {}
        self._lock = threading.Lock()

    # -- state -------------------------------------------------------------

    def _state_for(self, thread_id: str) -> dict[str, Any]:
        return self._states.setdefault(thread_id, {"groups": {}})

    def _snapshot(self, thread_id: str | None) -> dict[str, Any]:
        """Return the current state for *thread_id* (merged view for ``None``)."""
        if thread_id is None:
            merged: dict[str, Any] = {"groups": {}}
            for st in self._states.values():
                merged["groups"].update(st["groups"])
            return merged
        return self._states.get(thread_id, {"groups": {}})

    def apply(self, thread_id: str, event: StreamEvent) -> None:
        """Fold *event* into *thread_id*'s state and notify subscribers."""
        with self._lock:
            state = self._state_for(thread_id)
            changed = _update_activity_state(state, event)
            if not changed:
                return
            payload = f"data: {json.dumps(state)}\n\n"
            all_payload = f"data: {json.dumps(self._snapshot(None))}\n\n"
            thread_subs = list(self._subscribers.get(thread_id, []))
            all_subs = list(self._subscribers.get(self._ALL, []))
        self._deliver(thread_id, thread_subs, payload)
        self._deliver(self._ALL, all_subs, all_payload)

    # -- subscribers -------------------------------------------------------

    def subscribe(self, thread_id: str | None) -> tuple[asyncio.Queue[str], str]:
        """Register a subscriber. Returns the queue and an initial snapshot payload."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.setdefault(thread_id, []).append(q)
            initial = f"data: {json.dumps(self._snapshot(thread_id))}\n\n"
        return q, initial

    def unsubscribe(self, thread_id: str | None, q: asyncio.Queue[str]) -> None:
        with self._lock:
            subs = self._subscribers.get(thread_id)
            if subs and q in subs:
                subs.remove(q)

    def _deliver(
        self, thread_id: str | None, subs: list[asyncio.Queue[str]], payload: str
    ) -> None:
        dead: list[asyncio.Queue[str]] = []
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        if dead:
            with self._lock:
                live = self._subscribers.get(thread_id)
                if live:
                    for q in dead:
                        if q in live:
                            live.remove(q)
