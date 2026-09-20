"""The activity pump folds a burst of events into one snapshot.

Snapshots carry ``replace=True``, so only the last one a burst produces is ever
rendered. Emitting the intermediate ones cost a deep copy of the whole tree and
a full serialisation each — the dominant share of a long run's SSE bytes, and
the reason runs were reaching AgentCore's 100 MB response cap.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters.agui import _make_activity_pump
from kaboo_workflows.types import EventType, StreamEvent
from kaboo_workflows.wire import EventQueue


def _queue(maxsize: int = 1000) -> EventQueue:
    return EventQueue(asyncio.Queue(maxsize=maxsize), entry_name="main")


def _group_start(thread_id: str = "t1", group: str = "g1") -> StreamEvent:
    return StreamEvent(
        type=EventType.STREAM_GROUP_START,
        agent_name="worker",
        data={
            "stream_group": group,
            "stream_title": "Worker",
            "agent_name": "worker",
            "thread_id": thread_id,
        },
    )


def _token(text: str, thread_id: str = "t1", group: str = "g1") -> StreamEvent:
    return StreamEvent(
        type=EventType.TOKEN,
        agent_name="worker",
        data={"stream_group": group, "text": text, "thread_id": thread_id},
    )


async def _drain(eq: EventQueue, merged: asyncio.Queue, *, deltas: bool = False) -> list:
    """Run the pump until it exits, then return everything it emitted."""
    registry = ActivityRegistry()
    pump, _flush = _make_activity_pump(
        eq, registry, "main", merged, entry_inline=False, deltas=deltas
    )
    await asyncio.wait_for(pump(), timeout=5)
    return [merged.get_nowait() for _ in range(merged.qsize())]


async def test_a_burst_of_tokens_produces_one_snapshot() -> None:
    eq = _queue()
    merged: asyncio.Queue = asyncio.Queue()

    eq.put_event(_group_start())
    for chunk in ("Hel", "lo ", "wor", "ld"):
        eq.put_event(_token(chunk))
    await eq.close()

    emitted = await _drain(eq, merged)

    assert len(emitted) == 1
    assert emitted[0].content["groups"]["g1"]["timeline"] == [
        {"type": "text", "text": "Hello world"}
    ]


async def test_each_touched_thread_gets_its_own_snapshot() -> None:
    """Coalescing folds across threads, but a snapshot is per conversation."""
    eq = _queue()
    merged: asyncio.Queue = asyncio.Queue()

    for thread_id in ("t1", "t2"):
        eq.put_event(_group_start(thread_id))
        eq.put_event(_token("hi", thread_id))
    await eq.close()

    emitted = await _drain(eq, merged)

    assert {e.message_id for e in emitted} == {
        "kaboo.activity.t1",
        "kaboo.activity.t2",
    }
    for event in emitted:
        assert event.replace is True


async def test_the_sentinel_is_not_swallowed_by_the_drain() -> None:
    """The pump must still exit when the close lands inside a burst."""
    eq = _queue()
    merged: asyncio.Queue = asyncio.Queue()

    eq.put_event(_group_start())
    eq.put_event(_token("x"))
    await eq.close()

    emitted = await _drain(eq, merged)

    assert len(emitted) == 1


async def test_a_burst_with_no_stream_group_emits_nothing() -> None:
    """SESSION_END and friends carry no group, so they fold to no snapshot."""
    eq = _queue()
    merged: asyncio.Queue = asyncio.Queue()

    await eq.close()  # emits SESSION_END, then the sentinel

    emitted = await _drain(eq, merged)

    assert emitted == []


async def test_the_timeline_references_tools_rather_than_embedding_them() -> None:
    """The tool lives once, in ``tools``; the timeline holds its id.

    Appending the same dict to both is free in memory but JSON has no
    references, so it serialised every input and result twice per snapshot.
    """
    eq = _queue()
    merged: asyncio.Queue = asyncio.Queue()

    eq.put_event(_group_start())
    eq.put_event(
        StreamEvent(
            type=EventType.TOOL_START,
            agent_name="worker",
            data={
                "stream_group": "g1",
                "thread_id": "t1",
                "tool_use_id": "tc-1",
                "tool_name": "run_sql",
                "tool_input": {"sql": "select 1"},
            },
        )
    )
    eq.put_event(
        StreamEvent(
            type=EventType.TOOL_END,
            agent_name="worker",
            data={
                "stream_group": "g1",
                "thread_id": "t1",
                "tool_use_id": "tc-1",
                "status": "done",
                "tool_result": "one row",
            },
        )
    )
    await eq.close()

    emitted = await _drain(eq, merged)
    group = emitted[0].content["groups"]["g1"]

    assert group["timeline"] == [{"type": "tool", "toolUseId": "tc-1"}]
    assert group["tools"] == [
        {
            "toolUseId": "tc-1",
            "toolName": "run_sql",
            "toolLabel": "",
            "toolInput": {"sql": "select 1"},
            "status": "done",
            "toolResult": "one row",
        }
    ]
    # The result is carried once, not once per place the tool is referenced.
    assert json.dumps(group).count("one row") == 1


async def test_group_text_is_carried_once_in_the_timeline() -> None:
    """A flat ``tokens`` duplicate of the timeline text used to ride along.

    Nothing read it, and it cost its full length in every snapshot and, once
    deltas landed, in every patch — so the timeline is the only copy.
    """
    eq = _queue()
    merged: asyncio.Queue = asyncio.Queue()

    eq.put_event(_group_start())
    for _ in range(50):
        eq.put_event(_token("x" * 200))
    await eq.close()

    emitted = await _drain(eq, merged)
    group = emitted[0].content["groups"]["g1"]

    assert "tokens" not in group
    assert len(group["timeline"][0]["text"]) == 50 * 200


@pytest.mark.parametrize("burst", [2, 50, 200])
async def test_snapshot_count_does_not_grow_with_burst_size(burst: int) -> None:
    eq = _queue()
    merged: asyncio.Queue = asyncio.Queue(maxsize=0)

    eq.put_event(_group_start())
    for i in range(burst):
        eq.put_event(_token(f"{i} "))
    await eq.close()

    emitted = await _drain(eq, merged)

    assert len(emitted) == 1
