from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ag_ui.core import EventType


@dataclass
class _Message:
    id: str
    role: str
    text: str = ""
    calls: list[str] = field(default_factory=list)


def drawn(events: list[Any], run: str | None = None) -> list[str]:
    messages: list[_Message] = []

    def find(message_id: str | None) -> _Message | None:
        return next((m for m in messages if m.id == message_id), None)

    for event in events:
        if getattr(event, "subagent_run_id", None) != run:
            continue
        if event.type == EventType.TEXT_MESSAGE_START and find(event.message_id) is None:
            messages.append(_Message(event.message_id, "assistant"))
        elif event.type == EventType.TEXT_MESSAGE_CONTENT and (message := find(event.message_id)):
            message.text += event.delta
        elif event.type == EventType.REASONING_MESSAGE_START:
            messages.append(_Message(event.message_id, "reasoning"))
        elif event.type == EventType.TOOL_CALL_START:
            owner = find(event.parent_message_id) if event.parent_message_id else None
            if owner is None or owner.role != "assistant":
                owner_id = event.tool_call_id if owner else event.parent_message_id
                owner = _Message(owner_id or event.tool_call_id, "assistant")
                messages.append(owner)
            owner.calls.append(event.tool_call_name)
        elif event.type == EventType.TOOL_CALL_RESULT:
            messages.append(_Message(event.message_id, "tool"))

    shown: list[str] = []
    for message in messages:
        if message.role != "assistant":
            shown.append(message.role)
            continue
        shown.extend([message.text] if message.text else [])
        shown.extend(f"call {name}" for name in message.calls)
    return shown
