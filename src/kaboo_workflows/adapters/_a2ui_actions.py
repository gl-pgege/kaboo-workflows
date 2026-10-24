"""An A2UI surface's action, as the person's next turn.

CopilotKit sends an action on a rendered surface as ``forwardedProps.a2uiAction``
with no new message, and the A2UI middleware records it as a ``log_a2ui_event``
tool call and result the agent never made. The agent reads it instead as a user
turn that says what the person did.
"""

from __future__ import annotations

import uuid
from typing import Any

from ag_ui.core import RunAgentInput, UserMessage

LOG_A2UI_EVENT = "log_a2ui_event"


def _plain(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}: {_plain(item)}" for key, item in value.items())
    if isinstance(value, list):
        return ", ".join(_plain(item) for item in value)
    return str(value)


def describe_action(action: dict[str, Any]) -> str:
    name = action.get("name") or "an action"
    text = f'I used "{name}" on the "{action.get("surfaceId") or "inline"}" surface'
    context = action.get("context")
    if isinstance(context, dict) and context:
        text += f" ({_plain(context)})"
    return f"{text}."


def take_a2ui_action(input_data: RunAgentInput) -> bool:
    props = input_data.forwarded_props if isinstance(input_data.forwarded_props, dict) else {}
    action = (props.get("a2uiAction") or {}).get("userAction")
    if not isinstance(action, dict):
        return False
    logged = {
        call.id
        for message in input_data.messages or []
        if getattr(message, "role", None) == "assistant"
        for call in getattr(message, "tool_calls", None) or []
        if call.function.name == LOG_A2UI_EVENT
    }
    kept = [
        message
        for message in input_data.messages or []
        if not (
            getattr(message, "role", None) == "assistant"
            and (calls := getattr(message, "tool_calls", None))
            and all(call.id in logged for call in calls)
        )
        and getattr(message, "tool_call_id", None) not in logged
    ]
    kept.append(
        UserMessage(id=f"a2ui-action-{uuid.uuid4()}", role="user", content=describe_action(action))
    )
    input_data.messages = kept
    return True
