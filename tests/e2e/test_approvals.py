"""End-to-end tool approvals: a gated call waits, and only an approval runs it.

Driven through real agents (``ScriptedModel``) and the ``Pipeline`` harness, plus
the AG-UI endpoint itself for the one behaviour that lives there: a message sent
while a request waits skips it and starts a new turn.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from ag_ui.core import (
    AssistantMessage,
    FunctionCall,
    ResumeEntry,
    RunAgentInput,
    ToolCall,
    UserMessage,
)
from ag_ui.core import EventType as AGUIEventType
from fastapi import FastAPI

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters.agui import _add_kaboo_endpoint, _build_session
from kaboo_workflows.config import parse_config_sources, resolve_infra, validate_raw_config
from kaboo_workflows.testing import Pipeline
from tests.fakes.gated_tools import CALLS

CONFIGS = Path(__file__).parent / "configs"
GATE = str(CONFIGS / "approval_gate.yaml")
GRANT = {"tool": "submit_report", "subject": "submit_report", "scope": "thread"}


@pytest.fixture(autouse=True)
def _fresh_calls() -> Iterator[None]:
    CALLS.clear()
    yield
    CALLS.clear()


def _gate(result: Any) -> Any:
    outcome = result.run_outcome()
    assert getattr(outcome, "type", None) == "interrupt", "the run did not pause"
    return list(outcome.interrupts)[0]


async def test_a_gated_call_waits_and_the_host_is_told_what_it_waits_on() -> None:
    r1 = await Pipeline(GATE).turn("file it", thread_id="a1", run_id="r1")

    gate = _gate(r1)
    assert CALLS == []
    approvals = r1.state()["kaboo_session"]["approvals"]
    assert [a["interruptId"] for a in approvals] == [gate.id]
    assert approvals[0]["tool"] == "submit_report"
    assert approvals[0]["input"] == {"title": "Q3 draft"}


@pytest.mark.parametrize(
    ("props", "scopes"),
    [({}, ["once"]), ({"kaboo_grants": []}, ["once", "thread"])],
)
async def test_wider_scopes_are_offered_only_by_a_host_that_remembers(
    props: dict[str, Any], scopes: list[str]
) -> None:
    r1 = await Pipeline(GATE).turn("file it", thread_id="a2", run_id="r1", forwarded_props=props)
    assert (_gate(r1).metadata or {})["scopes"] == scopes


async def test_an_approval_runs_the_call() -> None:
    pipe = Pipeline(GATE)
    gate = _gate(await pipe.turn("file it", thread_id="a3", run_id="r1"))

    r2 = await pipe.turn(
        thread_id="a3",
        run_id="r2",
        resume=[{"interruptId": gate.id, "status": "resolved", "payload": {"status": "approved"}}],
    )

    assert not r2.errored() and r2.finished()
    assert CALLS == [{"title": "Q3 draft"}]


async def test_a_remembered_permission_runs_the_call_without_asking() -> None:
    r1 = await Pipeline(GATE).turn(
        "file it", thread_id="a4", run_id="r1", forwarded_props={"kaboo_grants": [GRANT]}
    )

    assert getattr(r1.run_outcome(), "type", None) != "interrupt"
    assert r1.finished()
    assert CALLS == [{"title": "Q3 draft"}]


async def test_a_denial_does_not_run_the_call_and_the_agent_hears_why() -> None:
    pipe = Pipeline(GATE)
    gate = _gate(await pipe.turn("file it", thread_id="a5", run_id="r1"))

    r2 = await pipe.turn(
        thread_id="a5",
        run_id="r2",
        resume=[
            {
                "interruptId": gate.id,
                "status": "resolved",
                "payload": {"status": "cancelled", "reason": "use the final title"},
            }
        ],
    )

    assert not r2.errored() and r2.finished()
    assert CALLS == []
    results = r2.of_type(AGUIEventType.TOOL_CALL_RESULT)
    assert any("use the final title" in r.content for r in results)


async def test_a_skipped_request_does_not_run_the_call() -> None:
    pipe = Pipeline(GATE)
    gate = _gate(await pipe.turn("file it", thread_id="a6", run_id="r1"))

    r2 = await pipe.turn(
        thread_id="a6", run_id="r2", resume=[{"interruptId": gate.id, "status": "cancelled"}]
    )

    assert not r2.errored() and r2.finished()
    assert CALLS == []


async def test_the_decision_is_recorded_on_the_tool_activity_entry() -> None:
    pipe = Pipeline(str(CONFIGS / "approval_delegate.yaml"))
    gate = _gate(await pipe.turn("file it", thread_id="a7", run_id="r1"))

    r2 = await pipe.turn(
        thread_id="a7",
        run_id="r2",
        resume=[{"interruptId": gate.id, "status": "resolved", "payload": {"status": "approved"}}],
    )

    assert CALLS == [{"title": "Q3 draft"}]
    filer = next(g for g in r2.groups.values() if g["agentName"] == "filer")
    tool = next(t for t in filer["tools"] if t["toolName"] == "submit_report")
    assert tool["approval"] == {"decision": "approved", "by": "person", "scope": "once"}


async def test_the_resumed_run_joins_the_runs_of_the_turns_groups() -> None:
    pipe = Pipeline(str(CONFIGS / "approval_delegate.yaml"))
    gate = _gate(await pipe.turn("file it", thread_id="a8", run_id="r1"))

    r2 = await pipe.turn(
        thread_id="a8",
        run_id="r2",
        resume=[{"interruptId": gate.id, "status": "resolved", "payload": {"status": "approved"}}],
    )

    assert r2.groups
    for group in r2.groups.values():
        assert (group["runId"], group["runIds"]) == ("r1", ["r1", "r2"])


class _StubRequest:
    """Only what the endpoint reads from a request when no auth verifier is set."""

    headers: dict[str, str] = {}


def _events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line[len("data: ") :])
        for frame in body.split("\n\n")
        for line in frame.splitlines()
        if line.startswith("data: ")
    ]


async def _post(endpoint: Any, input_data: RunAgentInput) -> list[dict[str, Any]]:
    response = await endpoint(input_data, _StubRequest())
    return _events("".join([chunk async for chunk in response.body_iterator]))


async def test_a_message_sent_while_a_request_waits_skips_it_and_starts_a_new_turn() -> None:
    app_config = validate_raw_config(parse_config_sources(GATE))
    session = _build_session(app_config, resolve_infra(app_config))
    app = FastAPI()
    _add_kaboo_endpoint(app, lambda _input: session, ActivityRegistry(), "/invocations")
    endpoint = cast("Any", app.routes[-1]).endpoint

    first = await _post(
        endpoint,
        RunAgentInput(
            thread_id="s1",
            run_id="r1",
            state={},
            messages=[UserMessage(id="m1", role="user", content="file it")],
            tools=[],
            context=[],
            forwarded_props={},
        ),
    )
    paused_call = next(e for e in first if e["type"] == "TOOL_CALL_START")["toolCallId"]
    gate_id = next(e for e in first if e["type"] == "RUN_FINISHED")["outcome"]["interrupts"][0][
        "id"
    ]

    second = await _post(
        endpoint,
        RunAgentInput(
            thread_id="s1",
            run_id="r2",
            state={},
            messages=[
                UserMessage(id="m1", role="user", content="file it"),
                AssistantMessage(
                    id="a1",
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id=paused_call,
                            type="function",
                            function=FunctionCall(
                                name="submit_report", arguments='{"title": "Q3 draft"}'
                            ),
                        )
                    ],
                ),
                UserMessage(id="m2", role="user", content="actually, file the final one"),
            ],
            tools=[],
            context=[],
            forwarded_props={},
            resume=[ResumeEntry(interrupt_id=gate_id, status="cancelled")],
        ),
    )

    assert not [e for e in second if e["type"] == "RUN_ERROR"]
    closed = [
        e for e in second if e["type"] == "TOOL_CALL_RESULT" and e["toolCallId"] == paused_call
    ]
    assert len(closed) == 1, "the waiting call is closed once, as skipped"
    assert "".join(e["delta"] for e in second if e["type"] == "TEXT_MESSAGE_CONTENT")
    assert CALLS == []
    activity = [e for e in second if e["type"] == "ACTIVITY_SNAPSHOT"][-1]["content"]
    paused_group = next(
        g
        for g in activity["groups"].values()
        if any(t["toolUseId"] == paused_call for t in g["tools"])
    )
    assert paused_group["status"] == "completed"
    assert paused_group["tools"][0]["approval"]["decision"] == "skipped"
