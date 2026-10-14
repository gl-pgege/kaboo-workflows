"""The tool approval gate, its resume answers and its host-facing descriptors.

Unit level, at the hook seam: which calls wait for approval, what a request
offers, which answers run a call, and how a decision is recorded. The wiring
through real agents is covered in ``tests/e2e/test_approvals.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ag_ui.core.types import Interrupt
from strands.hooks.events import BeforeToolCallEvent

from kaboo_workflows._context import SessionExchange, set_forwarded_props, set_session_exchange
from kaboo_workflows.adapters._interrupts import (
    approval_descriptors,
    build_resume_responses,
    map_strands_interrupt_to_agui,
)
from kaboo_workflows.config.schema import InterruptDef
from kaboo_workflows.hooks import GateRule, InterruptHook
from kaboo_workflows.hooks.interrupt_hook import APPROVALS_STATE_KEY


class _Paused(Exception):
    """Stands in for strands' InterruptException: the run pauses here."""


class _GateEvent:
    """Stands in for BeforeToolCallEvent at the hook seam.

    ``interrupt`` records the request and either pauses (no answer yet) or
    returns the answer, as strands does on resume.
    """

    def __init__(self, tool_name: str, *, answer: Any = None, tool: Any = None) -> None:
        self.tool_use = {"toolUseId": "use-1", "name": tool_name, "input": {"id": "x"}}
        self.selected_tool = tool
        self.invocation_state: dict[str, Any] = {}
        self.agent = None
        self.cancel_tool: str | bool = False
        self.requests: list[dict[str, Any]] = []
        self._answer = answer

    def interrupt(self, *, name: str, reason: dict[str, Any]) -> Any:
        self.requests.append(reason)
        if self._answer is None:
            raise _Paused
        return self._answer

    @property
    def decision(self) -> dict[str, Any] | None:
        return self.invocation_state.get(APPROVALS_STATE_KEY, {}).get("use-1")


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    set_session_exchange(None)
    set_forwarded_props(None)
    yield
    set_session_exchange(None)
    set_forwarded_props(None)


def _fire(hook: InterruptHook, event: _GateEvent) -> _GateEvent:
    try:
        hook._on_before_tool(cast(BeforeToolCallEvent, event))  # noqa: SLF001 — unit seam; e2e covers the wiring
    except _Paused:
        pass
    return event


def _mcp_tool(*, read_only: bool | None = None, title: str | None = None) -> Any:
    annotations = None if read_only is None else SimpleNamespace(readOnlyHint=read_only)
    return SimpleNamespace(mcp_tool=SimpleNamespace(annotations=annotations, title=title))


def _host_remembers(grants: list[dict[str, Any]] | None = None) -> None:
    set_session_exchange(SessionExchange(publishes_approvals=True))
    set_forwarded_props({"kaboo_grants": grants or []})


def test_a_matching_call_pauses_with_a_request_describing_it() -> None:
    event = _fire(InterruptHook(tools=["submit_report"]), _GateEvent("submit_report"))

    request = event.requests[0]
    assert request["type"] == "approval"
    assert request["tool_name"] == "submit_report"
    assert request["tool_input"] == {"id": "x"}
    assert request["subject"] == {"key": "submit_report", "label": "submit_report"}
    assert request["scopes"] == ["once"]


@pytest.mark.parametrize(
    ("pattern", "tool_name", "gated"),
    [
        ("merchant___*", "merchant___run_mutation", True),
        ("merchant___*", "shopper___run_mutation", False),
        ("submit_report", "submit_report", True),
        ("*", "ask_user", False),
    ],
)
def test_names_and_globs_select_the_gated_calls(pattern: str, tool_name: str, gated: bool) -> None:
    event = _fire(InterruptHook(tools=[pattern]), _GateEvent(tool_name))
    assert bool(event.requests) is gated


@pytest.mark.parametrize(
    ("tool", "gated"),
    [
        (_mcp_tool(read_only=True), False),
        (_mcp_tool(read_only=False), True),
        (_mcp_tool(), True),
        (None, True),
    ],
)
def test_skip_read_only_lets_only_annotated_read_only_tools_through(tool: Any, gated: bool) -> None:
    event = _fire(
        InterruptHook(tools=["*"], skip_read_only=True), _GateEvent("run_query", tool=tool)
    )
    assert bool(event.requests) is gated


def test_the_tool_title_labels_the_default_subject() -> None:
    event = _fire(
        InterruptHook(tools=["*"]), _GateEvent("update_price", tool=_mcp_tool(title="Update price"))
    )
    assert event.requests[0]["subject"] == {"key": "update_price", "label": "Update price"}


def test_wider_scopes_need_a_host_that_remembers() -> None:
    hook = InterruptHook(tools=["submit_report"], remember="always")

    alone = _fire(hook, _GateEvent("submit_report"))
    _host_remembers()
    hosted = _fire(hook, _GateEvent("submit_report"))

    assert alone.requests[0]["scopes"] == ["once"]
    assert hosted.requests[0]["scopes"] == ["once", "thread", "always"]


def test_grants_without_a_run_that_publishes_its_requests_offer_only_once() -> None:
    set_forwarded_props({"kaboo_grants": []})
    event = _fire(
        InterruptHook(tools=["submit_report"], remember="thread"), _GateEvent("submit_report")
    )
    assert event.requests[0]["scopes"] == ["once"]


def test_a_per_tool_remember_overrides_the_agent_default() -> None:
    _host_remembers()
    hook = InterruptHook(
        tools=[GateRule(pattern="delete_*", remember="once"), "submit_report"], remember="thread"
    )

    risky = _fire(hook, _GateEvent("delete_store"))
    routine = _fire(hook, _GateEvent("submit_report"))

    assert risky.requests[0]["scopes"] == ["once"]
    assert routine.requests[0]["scopes"] == ["once", "thread"]


def test_a_deadline_rides_the_request() -> None:
    event = _fire(
        InterruptHook(tools=["submit_report"], ttl_seconds=3600), _GateEvent("submit_report")
    )

    expires = datetime.fromisoformat(event.requests[0]["expiresAt"].replace("Z", "+00:00"))
    remaining = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 3590 < remaining <= 3600


def test_no_deadline_by_default() -> None:
    event = _fire(InterruptHook(tools=["submit_report"]), _GateEvent("submit_report"))
    assert "expiresAt" not in event.requests[0]


def test_a_subject_hook_decides_what_a_decision_covers() -> None:
    def by_record(tool_name: str, tool_input: dict[str, Any]) -> dict[str, str]:
        return {"key": f"{tool_name}:{tool_input['id']}", "label": f"Record {tool_input['id']}"}

    event = _fire(
        InterruptHook(tools=[GateRule(pattern="*", subject=by_record)]), _GateEvent("update")
    )
    assert event.requests[0]["subject"] == {"key": "update:x", "label": "Record x"}


def test_a_subject_hook_returning_none_needs_no_approval() -> None:
    event = _fire(
        InterruptHook(tools=[GateRule(pattern="*", subject=lambda _n, _i: None)]),
        _GateEvent("update"),
    )
    assert event.requests == []
    assert event.cancel_tool is False


def test_a_failing_subject_hook_still_asks_about_the_whole_tool() -> None:
    def broken(_name: str, _input: dict[str, Any]) -> Any:
        raise RuntimeError("boom")

    event = _fire(
        InterruptHook(tools=[GateRule(pattern="*", subject=broken)]), _GateEvent("update")
    )
    assert event.requests[0]["subject"]["key"] == "update"


def test_a_remembered_permission_runs_the_call_without_asking() -> None:
    _host_remembers([{"tool": "submit_report", "subject": "submit_report", "scope": "thread"}])

    event = _fire(InterruptHook(tools=["submit_report"]), _GateEvent("submit_report"))

    assert event.requests == []
    assert event.cancel_tool is False
    assert event.decision == {"decision": "approved", "by": "permission", "scope": "thread"}


@pytest.mark.parametrize(
    "grant",
    [
        {"tool": "submit_report", "subject": "other", "scope": "always"},
        {"tool": "other_tool", "subject": "submit_report", "scope": "always"},
        {"tool": "submit_report", "subject": "submit_report", "scope": "once"},
    ],
)
def test_a_permission_for_something_else_still_asks(grant: dict[str, Any]) -> None:
    _host_remembers([grant])
    event = _fire(InterruptHook(tools=["submit_report"]), _GateEvent("submit_report"))
    assert event.requests


def test_an_approval_runs_the_call_and_records_its_scope() -> None:
    event = _fire(
        InterruptHook(tools=["submit_report"]),
        _GateEvent("submit_report", answer={"status": "approved", "scope": "thread"}),
    )
    assert event.cancel_tool is False
    assert event.tool_use["input"] == {"id": "x"}
    assert event.decision == {"decision": "approved", "by": "person", "scope": "thread"}


def test_an_approval_with_edits_runs_the_edited_call() -> None:
    event = _fire(
        InterruptHook(tools=["submit_report"]),
        _GateEvent("submit_report", answer={"status": "approved", "tool_input": {"id": "y"}}),
    )
    assert event.cancel_tool is False
    assert event.tool_use["input"] == {"id": "y"}
    assert event.tool_use["toolUseId"] == "use-1"


@pytest.mark.parametrize(
    ("answer", "decision"),
    [
        ({"status": "cancelled", "reason": "wrong title"}, "denied"),
        ({"status": "cancelled"}, "denied"),
        ({"status": "cancelled", "skipped": True}, "skipped"),
        ({"status": "cancelled", "expired": True}, "expired"),
        ({"status": "rejected"}, "skipped"),
        ({"answer": "yes"}, "skipped"),
        ("approve", "skipped"),
    ],
)
def test_only_an_approval_runs_the_call(answer: Any, decision: str) -> None:
    event = _fire(
        InterruptHook(tools=["submit_report"]), _GateEvent("submit_report", answer=answer)
    )
    assert event.cancel_tool
    assert event.decision is not None
    assert event.decision["decision"] == decision


def test_a_denial_hands_the_reason_to_the_model() -> None:
    event = _fire(
        InterruptHook(tools=["submit_report"]),
        _GateEvent("submit_report", answer={"status": "cancelled", "reason": "wrong title"}),
    )
    assert isinstance(event.cancel_tool, str)
    assert "wrong title" in event.cancel_tool
    assert event.decision == {"decision": "denied", "by": "person", "reason": "wrong title"}


def test_the_persons_answer_wins_over_a_permission_it_created() -> None:
    """Resuming with "allow for this chat" also delivers that permission; the edit still applies."""
    _host_remembers([{"tool": "submit_report", "subject": "submit_report", "scope": "thread"}])
    event = _GateEvent("submit_report")
    answered = SimpleNamespace(
        id="v1:before_tool_call:use-1:abc",
        name=":gate:submit_report",
        response={"status": "approved", "scope": "thread", "tool_input": {"id": "edited"}},
    )
    event.agent = SimpleNamespace(_interrupt_state=SimpleNamespace(interrupts={"i": answered}))

    _fire(InterruptHook(tools=["submit_report"]), event)

    assert event.tool_use["input"] == {"id": "edited"}
    assert event.decision == {"decision": "approved", "by": "person", "scope": "thread"}


def _interrupt(interrupt_id: str, reason: Any = None) -> Any:
    return SimpleNamespace(id=interrupt_id, reason=reason or {"type": "approval"})


def _responses(pending: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        r["interruptResponse"]["interruptId"]: r["interruptResponse"]["response"]
        for r in build_resume_responses(pending, entries)
    }


def test_resolved_answers_pass_through_and_a_bare_one_approves() -> None:
    pending = {"a": _interrupt("a"), "b": _interrupt("b")}
    out = _responses(
        pending,
        [
            {
                "interruptId": "a",
                "status": "resolved",
                "payload": {"status": "approved", "scope": "thread"},
            },
            {"interruptId": "b", "status": "resolved"},
        ],
    )
    assert out == {"a": {"status": "approved", "scope": "thread"}, "b": {"status": "approved"}}


def test_cancelled_and_unaddressed_requests_are_skipped_never_approved() -> None:
    pending = {"a": _interrupt("a"), "b": _interrupt("b")}
    out = _responses(pending, [{"interruptId": "a", "status": "cancelled"}])
    assert out["a"]["status"] == "cancelled" and out["a"]["skipped"] is True
    assert out["b"]["status"] == "cancelled" and out["b"]["skipped"] is True


def test_an_expired_request_cannot_be_approved() -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    pending = {
        "late": _interrupt("late", {"type": "approval", "expiresAt": past}),
        "on_time": _interrupt("on_time", {"type": "approval", "expiresAt": future}),
    }
    approve = {"status": "resolved", "payload": {"status": "approved"}}
    out = _responses(
        pending, [{"interruptId": "late", **approve}, {"interruptId": "on_time", **approve}]
    )
    assert out["late"]["expired"] is True
    assert out["on_time"] == {"status": "approved"}


def test_the_deadline_stays_in_metadata_never_on_the_ag_ui_descriptor() -> None:
    """The AG-UI client locks a thread once a descriptor's expiresAt passes."""
    reason = {
        "type": "approval",
        "message": "Agent wants to call submit_report",
        "tool_name": "submit_report",
        "tool_input": {"id": "x"},
        "expiresAt": "2026-07-24T12:00:00Z",
    }
    descriptor = map_strands_interrupt_to_agui(_interrupt("v1:before_tool_call:use-1:abc", reason))

    parsed = Interrupt.model_validate(descriptor)
    assert parsed.expires_at is None
    assert parsed.metadata is not None and parsed.metadata["expiresAt"] == "2026-07-24T12:00:00Z"
    assert parsed.tool_call_id == "use-1"
    assert parsed.reason == "tool_call"


def test_a_custom_request_kind_reaches_the_ui_with_its_fields() -> None:
    reason = {"type": "files", "message": "Upload the Q3 workbook", "accept": [".xlsx"]}
    descriptor = map_strands_interrupt_to_agui(_interrupt("v1:tool_call:use-9:abc", reason))
    assert descriptor["metadata"] == reason
    assert descriptor["message"] == "Upload the Q3 workbook"


@pytest.mark.parametrize(
    ("reason", "message"),
    [
        ({"type": "form", "questions": [{"prompt": "Pick a datasource"}]}, "Pick a datasource"),
        ({"type": "form", "questions": []}, "Input required"),
    ],
)
def test_a_form_descriptor_leads_with_its_first_question(reason: Any, message: str) -> None:
    descriptor = map_strands_interrupt_to_agui(_interrupt("v1:tool_call:use-1:abc", reason))
    assert descriptor["reason"] == "input_required"
    assert descriptor["message"] == message


def test_hosts_get_a_descriptor_per_open_approval() -> None:
    approval = {
        "type": "approval",
        "tool_name": "submit_report",
        "tool_input": {"title": "Q3"},
        "subject": {"key": "submit_report", "label": "Submit report"},
        "scopes": ["once", "thread"],
        "expiresAt": "2026-07-24T12:00:00Z",
    }
    question = {"type": "form", "questions": [{"question": "Which quarter?"}]}

    descriptors = approval_descriptors(
        [
            _interrupt("v1:before_tool_call:use-1:abc", approval),
            _interrupt("v1:tool_call:use-2:def", question),
        ]
    )

    assert descriptors == [
        {
            "interruptId": "v1:before_tool_call:use-1:abc",
            "toolCallId": "use-1",
            "tool": "submit_report",
            "subject": {"key": "submit_report", "label": "Submit report"},
            "scopes": ["once", "thread"],
            "input": {"title": "Q3"},
            "expiresAt": "2026-07-24T12:00:00Z",
        }
    ]


def test_tools_accept_names_and_objects() -> None:
    interrupt = InterruptDef.model_validate(
        {"tools": ["merchant___*", {"name": "sql___run_query", "remember": "once"}]}
    )
    assert [t.name for t in interrupt.tools] == ["merchant___*", "sql___run_query"]
    assert interrupt.tools[1].remember == "once"
    assert interrupt.remember == "once"
    assert interrupt.skip_read_only is False


@pytest.mark.parametrize(
    "raw",
    [
        {"tools": ["t"], "ttl_seconds": 0},
        {"tools": ["t"], "remember": "forever"},
        {"tools": [{"name": "t", "remember": "sometimes"}]},
        {"tools": [{"name": ""}]},
        {"tools": [{"name": "t", "unknown": 1}]},
    ],
)
def test_invalid_interrupt_config_is_rejected(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        InterruptDef.model_validate(raw)


def test_an_unknown_scope_is_rejected_by_the_hook() -> None:
    with pytest.raises(ValueError):
        InterruptHook(tools=["t"], remember="forever")
