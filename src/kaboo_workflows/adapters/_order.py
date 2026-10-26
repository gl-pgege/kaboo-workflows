from __future__ import annotations

import uuid
from typing import Any

from ag_ui.core import EventType as AGUIEventType

_TEXT, _CALLS, _OTHER = "text", "calls", "other"


class AppendOnly:
    def __init__(self) -> None:
        self._newest: dict[str | None, tuple[str, str]] = {}
        self._seen: set[tuple[str | None, str]] = set()
        self._renamed: dict[tuple[str | None, str], str] = {}
        self._moved: dict[tuple[str | None, str], str] = {}

    def place(self, event: Any) -> Any:
        etype = getattr(event, "type", None)
        scope = getattr(event, "subagent_run_id", None)
        if etype == AGUIEventType.TEXT_MESSAGE_START:
            return self._text(scope, event)
        if etype in (AGUIEventType.TEXT_MESSAGE_CONTENT, AGUIEventType.TEXT_MESSAGE_END):
            key = (scope, event.message_id)
            renamed = self._renamed.get(key)
            if etype == AGUIEventType.TEXT_MESSAGE_END:
                self._renamed.pop(key, None)
            return event.model_copy(update={"message_id": renamed}) if renamed else event
        if etype == AGUIEventType.TOOL_CALL_START:
            return self._call(scope, event)
        if etype in (AGUIEventType.REASONING_MESSAGE_START, AGUIEventType.TOOL_CALL_RESULT):
            self._open(scope, event.message_id, _OTHER)
        elif (
            etype == AGUIEventType.ACTIVITY_SNAPSHOT and (scope, event.message_id) not in self._seen
        ):
            self._open(scope, event.message_id, _OTHER)
        return event

    def _open(self, scope: str | None, message_id: str, kind: str) -> None:
        self._seen.add((scope, message_id))
        self._newest[scope] = (message_id, kind)

    def _text(self, scope: str | None, event: Any) -> Any:
        message_id = event.message_id
        continuing = self._newest.get(scope) == (message_id, _TEXT)
        if continuing or (scope, message_id) not in self._seen:
            self._open(scope, message_id, _TEXT)
            return event
        fresh = str(uuid.uuid4())
        self._renamed[(scope, message_id)] = fresh
        self._open(scope, fresh, _TEXT)
        return event.model_copy(update={"message_id": fresh})

    def _call(self, scope: str | None, event: Any) -> Any:
        parent = event.parent_message_id
        newest = self._newest.get(scope)
        if parent is None or (scope, parent) not in self._seen:
            self._open(scope, parent or event.tool_call_id, _CALLS)
            return event
        if newest is not None and newest[0] == parent and newest[1] != _OTHER:
            self._newest[scope] = (parent, _CALLS)
            return event
        moved = self._moved.get((scope, parent))
        if moved is None or newest is None or newest[0] != moved:
            moved = str(uuid.uuid4())
            self._moved[(scope, parent)] = moved
        self._open(scope, moved, _CALLS)
        return event.model_copy(update={"parent_message_id": moved})
