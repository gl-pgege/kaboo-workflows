"""Interrupt expiry (``ttl_seconds`` → AG-UI ``Interrupt.expiresAt``).

Covers the three seams end to end at unit level: the hook stamps
``expiresAt`` into the interrupt reason, the AG-UI mapper lifts it onto the
descriptor, and the config schema validates ``interrupt.ttl_seconds``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

import pytest
from ag_ui.core.types import Interrupt
from strands.hooks.events import BeforeToolCallEvent

from kaboo_workflows.adapters.agui import _map_strands_interrupt_to_agui
from kaboo_workflows.config.schema import InterruptDef
from kaboo_workflows.hooks.interrupt_hook import InterruptHook


class _FakeStrandsInterrupt:
    def __init__(self, interrupt_id: str, reason: Any) -> None:
        self.id = interrupt_id
        self.reason = reason


class _RecordingEvent:
    """Stands in for BeforeToolCallEvent: records the interrupt reason."""

    def __init__(self, tool_name: str, response: Any = None) -> None:
        self.tool_use = {"toolUseId": "use-1", "name": tool_name, "input": {"id": "x"}}
        self.cancel_tool: str | bool = False
        self.raised: list[dict[str, Any]] = []
        self._response = response

    def interrupt(self, *, name: str, reason: dict[str, Any]) -> Any:
        self.raised.append({"name": name, "reason": reason})
        return self._response


def _fire(
    hook: InterruptHook,
    tool_name: str = "transition_work_item",
    response: Any = None,
) -> _RecordingEvent:
    event = _RecordingEvent(tool_name, response)
    hook._on_before_tool(cast(BeforeToolCallEvent, event))  # noqa: SLF001 — unit seam; e2e covers the wiring
    return event


def test_hook_stamps_iso_expiry_when_ttl_configured() -> None:
    event = _fire(InterruptHook(tools=["transition_work_item"], ttl_seconds=3600))
    reason = event.raised[0]["reason"]
    expires = datetime.fromisoformat(reason["expiresAt"].replace("Z", "+00:00"))
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 3590 < remaining <= 3600
    assert reason["tool_name"] == "transition_work_item"
    assert reason["tool_input"] == {"id": "x"}


def test_hook_emits_no_expiry_by_default() -> None:
    event = _fire(InterruptHook(tools=["transition_work_item"]))
    assert "expiresAt" not in event.raised[0]["reason"]


def test_ungated_tool_is_untouched() -> None:
    event = _fire(InterruptHook(tools=["other_tool"], ttl_seconds=60))
    assert event.raised == []


def test_mapper_lifts_expiry_onto_the_descriptor() -> None:
    descriptor = _map_strands_interrupt_to_agui(
        _FakeStrandsInterrupt(
            "v1:before_tool_call:use-1:abc",
            {
                "type": "approval",
                "message": "Agent wants to call transition_work_item",
                "tool_name": "transition_work_item",
                "tool_input": {"id": "x"},
                "expiresAt": "2026-07-24T12:00:00Z",
            },
        )
    )
    assert descriptor["expiresAt"] == "2026-07-24T12:00:00Z"
    assert descriptor["toolCallId"] == "use-1"
    assert descriptor["reason"] == "tool_call"
    # The descriptor must validate against the AG-UI Interrupt model.
    parsed = Interrupt.model_validate(descriptor)
    assert parsed.expires_at == "2026-07-24T12:00:00Z"


def test_mapper_omits_expiry_when_absent() -> None:
    descriptor = _map_strands_interrupt_to_agui(
        _FakeStrandsInterrupt(
            "v1:before_tool_call:use-1:abc",
            {"type": "approval", "message": "m", "tool_name": "t", "tool_input": {}},
        )
    )
    assert "expiresAt" not in descriptor


def test_mapper_tolerates_form_questions_without_question_key() -> None:
    descriptor = _map_strands_interrupt_to_agui(
        _FakeStrandsInterrupt(
            "v1:tool_call:use-1:abc",
            {"type": "form", "questions": [{"prompt": "Pick a datasource"}]},
        )
    )
    assert descriptor["reason"] == "input_required"
    assert descriptor["message"] == "Pick a datasource"


def test_mapper_defaults_form_message_when_questions_are_empty() -> None:
    descriptor = _map_strands_interrupt_to_agui(
        _FakeStrandsInterrupt("v1:tool_call:use-1:abc", {"type": "form", "questions": []})
    )
    assert descriptor["message"] == "Input required"


def test_cancelled_response_cancels_the_tool() -> None:
    event = _fire(
        InterruptHook(tools=["transition_work_item"]),
        response={"status": "cancelled"},
    )
    assert event.cancel_tool == "User rejected this action."
    assert event.tool_use["input"] == {"id": "x"}


def test_approved_response_with_tool_input_overrides_the_call_arguments() -> None:
    event = _fire(
        InterruptHook(tools=["transition_work_item"]),
        response={"status": "approved", "tool_input": {"id": "x", "body": {"to": "done"}}},
    )
    assert event.cancel_tool is False
    assert event.tool_use["input"] == {"id": "x", "body": {"to": "done"}}
    assert event.tool_use["toolUseId"] == "use-1"


def test_plain_approval_keeps_the_original_arguments() -> None:
    event = _fire(
        InterruptHook(tools=["transition_work_item"]),
        response={"status": "approved"},
    )
    assert event.cancel_tool is False
    assert event.tool_use["input"] == {"id": "x"}


def test_non_dict_tool_input_is_ignored() -> None:
    event = _fire(
        InterruptHook(tools=["transition_work_item"]),
        response={"status": "approved", "tool_input": "not-a-dict"},
    )
    assert event.tool_use["input"] == {"id": "x"}


def test_schema_accepts_ttl_and_rejects_nonpositive() -> None:
    assert InterruptDef(tools=["t"], ttl_seconds=60).ttl_seconds == 60
    assert InterruptDef(tools=["t"]).ttl_seconds is None
    with pytest.raises(ValueError):
        InterruptDef(tools=["t"], ttl_seconds=0)
