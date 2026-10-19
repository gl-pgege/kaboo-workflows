"""A later delegation to an existing stream group gets its own group; a resume keeps it."""

from __future__ import annotations

import asyncio

import jsonpatch
from ag_ui.core import EventType as AGUIEventType

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters.agui import _make_activity_pump
from kaboo_workflows.types import EventType, StreamEvent
from kaboo_workflows.wire import EventQueue

EDITOR = "orchestrator.edit_dashboard"
REVIEWER = f"{EDITOR}.quality_reviewer"


def _wire_patch(event: object) -> list[dict]:
    """The patch as a client receives it: plain JSON Patch operations."""
    return event.model_dump(mode="json", by_alias=True, exclude_none=True)["patch"]  # ty: ignore[unresolved-attribute]


def _start(
    tool_call_id: str,
    run_id: str,
    *,
    turn_id: str | None = None,
    group: str = EDITOR,
    agent: str = "edit_dashboard",
    parent: str = "orchestrator",
    task: str = "edit the dashboard",
) -> StreamEvent:
    return StreamEvent(
        type=EventType.STREAM_GROUP_START,
        agent_name=agent,
        data={
            "stream_group": group,
            "stream_title": agent.replace("_", " ").title(),
            "agent_name": agent,
            "parent_group": parent,
            "tool_call_id": tool_call_id,
            "run_id": run_id,
            "turn_id": turn_id or run_id,
            "task": task,
            "thread_id": "t1",
        },
    )


def _reviewer_start(tool_call_id: str, run_id: str) -> StreamEvent:
    return _start(tool_call_id, run_id, group=REVIEWER, agent="quality_reviewer", parent=EDITOR)


def _event(kind: EventType, group: str = EDITOR, **data: object) -> StreamEvent:
    return StreamEvent(
        type=kind,
        agent_name="edit_dashboard",
        data={"stream_group": group, "thread_id": "t1", **data},
    )


def _by_call(groups: dict, tool_call_id: str) -> tuple[str, dict]:
    return next((key, g) for key, g in groups.items() if g["toolCallId"] == tool_call_id)


def test_a_new_delegation_starts_a_fresh_group_and_keeps_the_old_one() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("tc-1", "run-1", task="add a revenue chart"))
    reg.apply("t1", _event(EventType.TOKEN, text="Added the chart."))
    reg.apply("t1", _event(EventType.STREAM_GROUP_END, status="completed"))

    reg.apply("t1", _start("tc-2", "run-2", task="rename the chart"))
    reg.apply("t1", _event(EventType.TOKEN, text="Renaming."))

    groups = reg.snapshot("t1")["groups"]
    archived_key, archived = _by_call(groups, "tc-1")
    assert archived_key != EDITOR and "tc-1" in archived_key
    assert archived["runId"] == "run-1"
    assert archived["task"] == "add a revenue chart"
    assert archived["status"] == "completed"
    assert archived["timeline"] == [{"type": "text", "text": "Added the chart."}]
    fresh = groups[EDITOR]
    assert fresh["toolCallId"] == "tc-2"
    assert fresh["runId"] == fresh["turnId"] == "run-2"
    assert fresh["task"] == "rename the chart"
    assert fresh["status"] == "active"
    assert fresh["timeline"] == [{"type": "text", "text": "Renaming."}]


def test_the_fresh_group_is_exactly_what_a_first_start_would_create() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("tc-1", "run-1"))
    reg.apply("t1", _event(EventType.TOKEN, text="Done."))
    reg.apply("t1", _event(EventType.AGENT_COMPLETE, run_id="run-1", usage={"input_tokens": 9}))
    first_time = ActivityRegistry()

    reg.apply("t1", _start("tc-2", "run-2"))
    first_time.apply("t1", _start("tc-2", "run-2"))

    assert reg.snapshot("t1")["groups"][EDITOR] == first_time.snapshot("t1")["groups"][EDITOR]


def test_a_resume_of_the_same_delegation_reuses_its_group() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("tc-1", "run-1"))
    reg.apply("t1", _event(EventType.TOKEN, text="Waiting for approval."))
    reg.apply("t1", _event(EventType.INTERRUPT, interrupt_id="i-1", name="approve"))

    reg.apply("t1", _start("tc-1", "run-2"))

    groups = reg.snapshot("t1")["groups"]
    assert list(groups) == [EDITOR]
    assert groups[EDITOR]["status"] == "active"
    assert "interrupt" not in groups[EDITOR]
    assert groups[EDITOR]["timeline"] == [{"type": "text", "text": "Waiting for approval."}]


def test_a_resume_adds_its_run_to_the_turns_group() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("tc-1", "run-1"))
    reg.apply("t1", _event(EventType.AGENT_START, run_id="run-1", turn_id="run-1"))
    reg.apply("t1", _event(EventType.INTERRUPT, interrupt_id="i-1", name="approve"))

    reg.apply("t1", _event(EventType.AGENT_START, run_id="run-2", turn_id="run-1"))
    reg.apply("t1", _event(EventType.INTERRUPT, interrupt_id="i-2", name="approve"))
    reg.apply("t1", _start("tc-1", "run-3", turn_id="run-1"))
    reg.apply("t1", _event(EventType.AGENT_START, run_id="run-3", turn_id="run-1"))

    group = reg.snapshot("t1")["groups"][EDITOR]
    assert group["runId"] == group["turnId"] == "run-1"
    assert group["runIds"] == ["run-1", "run-2", "run-3"]


def test_groups_nested_under_the_old_delegation_move_with_it() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("tc-1", "run-1"))
    reg.apply("t1", _reviewer_start("tc-1r", "run-1"))
    reg.apply(
        "t1",
        _start("tc-1c", "run-1", group=f"{REVIEWER}.checker", agent="checker", parent=REVIEWER),
    )
    reg.apply("t1", _start("tc-9", "run-1", group=f"{EDITOR}_v2", agent="edit_dashboard_v2"))

    reg.apply("t1", _start("tc-2", "run-2"))
    reg.apply("t1", _reviewer_start("tc-2r", "run-2"))

    groups = reg.snapshot("t1")["groups"]
    archived_key, _ = _by_call(groups, "tc-1")
    old_reviewer_key, old_reviewer = _by_call(groups, "tc-1r")
    _, old_checker = _by_call(groups, "tc-1c")
    assert old_reviewer["parentGroup"] == archived_key
    assert old_checker["parentGroup"] == old_reviewer_key
    assert [g["toolCallId"] for g in groups.values() if g["parentGroup"] == EDITOR] == ["tc-2r"]
    assert groups[f"{EDITOR}_v2"]["toolCallId"] == "tc-9"
    assert groups[f"{EDITOR}_v2"]["parentGroup"] == "orchestrator"


async def test_the_client_patch_reproduces_the_move() -> None:
    queue = EventQueue(asyncio.Queue(maxsize=100), entry_name="main")
    merged: asyncio.Queue = asyncio.Queue()
    registry = ActivityRegistry()
    pump, _flush = _make_activity_pump(queue, registry, "main", merged)
    task = asyncio.create_task(pump())

    for event in (
        _start("tc-1", "run-1"),
        _reviewer_start("tc-1r", "run-1"),
        _event(EventType.TOKEN, text="Added the chart."),
        _event(EventType.STREAM_GROUP_END, status="completed"),
    ):
        queue.put_event(event)
    snapshot = await merged.get()
    for event in (_start("tc-2", "run-2"), _event(EventType.TOKEN, text="Renaming.")):
        queue.put_event(event)
    delta = await merged.get()
    await queue.close()
    await task

    assert snapshot.type == AGUIEventType.ACTIVITY_SNAPSHOT
    assert delta.type == AGUIEventType.ACTIVITY_DELTA
    client = jsonpatch.JsonPatch(_wire_patch(delta)).apply(snapshot.content)
    assert client == registry.snapshot("t1")
    assert client["groups"][EDITOR]["toolCallId"] == "tc-2"
    archived_key, _ = _by_call(client["groups"], "tc-1")
    assert _by_call(client["groups"], "tc-1r")[1]["parentGroup"] == archived_key
