"""Activity rides the stream as JSON Patches after the first snapshot.

Re-sending the whole tree on every change is what made a long run's byte count
grow with the square of its length, which is how runs reached AgentCore's
100 MB response cap. A patch costs the size of what changed.

The baseline lives in the pump, which is built per request, so every response
opens with a full snapshot: a client is never asked to patch a tree it has not
been given.
"""

from __future__ import annotations

import asyncio
import json

import jsonpatch
from ag_ui.core import EventType as AGUIEventType

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters.agui import _make_activity_pump
from kaboo_workflows.types import EventType, StreamEvent
from kaboo_workflows.wire import EventQueue


def _queue(maxsize: int = 1000) -> EventQueue:
    return EventQueue(asyncio.Queue(maxsize=maxsize), entry_name="main")


def _group_start(group: str = "g1") -> StreamEvent:
    return StreamEvent(
        type=EventType.STREAM_GROUP_START,
        agent_name="worker",
        data={
            "stream_group": group,
            "stream_title": "Worker",
            "agent_name": "worker",
            "thread_id": "t1",
        },
    )


def _token(text: str, group: str = "g1") -> StreamEvent:
    return StreamEvent(
        type=EventType.TOKEN,
        agent_name="worker",
        data={"stream_group": group, "text": text, "thread_id": "t1"},
    )


class _Pump:
    """Drives the pump a burst at a time, as the run loop would."""

    def __init__(self, *, deltas: bool = True) -> None:
        self.eq = _queue()
        self.merged: asyncio.Queue = asyncio.Queue()
        self.registry = ActivityRegistry()
        self._pump, self.flush = _make_activity_pump(
            self.eq, self.registry, "main", self.merged, entry_inline=False, deltas=deltas
        )
        self._task: asyncio.Task | None = None

    async def burst(self, *events: StreamEvent) -> list:
        """Queue *events*, let the pump drain them, and return what it emitted."""
        for event in events:
            self.eq.put_event(event)
        if self._task is None:
            self._task = asyncio.create_task(self._pump())
        # The pump wakes on a 0.15s timeout; give it room to drain and emit.
        await asyncio.sleep(0.35)
        return [self.merged.get_nowait() for _ in range(self.merged.qsize())]

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


async def test_the_first_emission_is_a_snapshot_and_the_rest_are_patches() -> None:
    pump = _Pump()
    try:
        first = await pump.burst(_group_start(), _token("hello"))
        second = await pump.burst(_token(" world"))
    finally:
        await pump.stop()

    assert [e.type for e in first] == [AGUIEventType.ACTIVITY_SNAPSHOT]
    assert [e.type for e in second] == [AGUIEventType.ACTIVITY_DELTA]


async def test_applying_the_patches_reproduces_the_tree() -> None:
    """What the client ends up holding must equal the registry's own state."""
    pump = _Pump()
    try:
        emitted = await pump.burst(_group_start(), _token("one"))
        for text in (" two", " three", " four"):
            emitted += await pump.burst(_token(text))
    finally:
        await pump.stop()

    client = emitted[0].content
    for event in emitted[1:]:
        client = jsonpatch.JsonPatch(event.patch).apply(client)

    assert client == pump.registry.snapshot("t1")
    assert client["groups"]["g1"]["timeline"] == [{"type": "text", "text": "one two three four"}]


async def test_a_patch_is_far_smaller_than_the_tree_it_updates() -> None:
    """The point of the exercise: cost tracks the change, not the tree."""
    pump = _Pump()
    try:
        # A tree with some bulk in it: a finished tool carrying a real payload.
        emitted = await pump.burst(
            _group_start(),
            StreamEvent(
                type=EventType.TOOL_START,
                agent_name="worker",
                data={
                    "stream_group": "g1",
                    "thread_id": "t1",
                    "tool_use_id": "tc-1",
                    "tool_name": "run_sql",
                    "tool_input": {"sql": "select * from t"},
                },
            ),
            StreamEvent(
                type=EventType.TOOL_END,
                agent_name="worker",
                data={
                    "stream_group": "g1",
                    "thread_id": "t1",
                    "tool_use_id": "tc-1",
                    "status": "done",
                    "tool_result": "x" * 20_000,
                },
            ),
        )
        follow_up = await pump.burst(_token("done"))
    finally:
        await pump.stop()

    tree = len(json.dumps(emitted[-1].content))
    patch = len(json.dumps(follow_up[0].patch))

    assert tree > 20_000
    assert patch < 500, f"patch was {patch} bytes against a {tree} byte tree"


async def test_deltas_off_keeps_sending_snapshots() -> None:
    pump = _Pump(deltas=False)
    try:
        first = await pump.burst(_group_start(), _token("hello"))
        second = await pump.burst(_token(" world"))
    finally:
        await pump.stop()

    assert [e.type for e in first] == [AGUIEventType.ACTIVITY_SNAPSHOT]
    assert [e.type for e in second] == [AGUIEventType.ACTIVITY_SNAPSHOT]
    assert second[0].content["groups"]["g1"]["timeline"][0]["text"] == "hello world"


async def test_the_final_flush_is_a_snapshot_not_a_patch() -> None:
    """The last word on a run is a whole tree, which cannot be misapplied."""
    pump = _Pump()
    try:
        await pump.burst(_group_start(), _token("hello"))
        await pump.burst(_token(" world"))
    finally:
        await pump.stop()

    final = pump.flush("t1")

    assert len(final) == 1
    assert final[0].type == AGUIEventType.ACTIVITY_SNAPSHOT
    assert final[0].content == pump.registry.snapshot("t1")


async def test_a_change_that_leaves_the_tree_identical_emits_nothing() -> None:
    pump = _Pump()
    try:
        await pump.burst(_group_start())
        # A token for a group that never started folds to no visible change.
        quiet = await pump.burst(_token("x", group="never-started"))
    finally:
        await pump.stop()

    assert quiet == []
