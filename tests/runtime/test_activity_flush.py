"""The activity pump's flush yields a final snapshot at stream end.

The entry agent's final ``AGENT_COMPLETE`` (carrying the run's token usage)
lands on the event queue just before ``RUN_FINISHED``. Even when the pump
task dequeues it in time, the snapshot it puts on the merged queue can land
after the stream loop has broken — so the flush drains what is left and
re-snapshots the registry, which is deterministic no matter which side got
to the event first.
"""

from __future__ import annotations

import asyncio

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters.agui import _make_activity_pump
from kaboo_workflows.types import EventType, StreamEvent
from kaboo_workflows.wire import EventQueue


def _queue() -> EventQueue:
    return EventQueue(asyncio.Queue(maxsize=100), entry_name="main")


def _entry_complete() -> StreamEvent:
    return StreamEvent(
        type=EventType.AGENT_COMPLETE,
        agent_name="main",
        data={
            "stream_group": "main",
            "run_id": "r1",
            "thread_id": "t1",
            "usage": {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13},
        },
    )


def test_flush_folds_queued_entry_usage_into_the_final_snapshot() -> None:
    eq = _queue()
    registry = ActivityRegistry()
    merged: asyncio.Queue = asyncio.Queue()
    _pump, flush = _make_activity_pump(eq, registry, "main", merged, entry_inline=False)

    eq.put_event(_entry_complete())
    snapshots = flush("t1")

    assert len(snapshots) == 1
    content = snapshots[0].content
    assert content["usageByRun"]["r1"]["totalTokens"] == 13
    assert "main" not in content["groups"]  # entry stream stays unrendered


def test_flush_emits_final_snapshot_even_when_pump_already_folded() -> None:
    """The pump may have consumed the event but lost its snapshot to the
    shutdown race — the flush must still deliver the folded state."""
    eq = _queue()
    registry = ActivityRegistry()
    merged: asyncio.Queue = asyncio.Queue()
    _pump, flush = _make_activity_pump(eq, registry, "main", merged, entry_inline=False)

    registry.apply_usage("t1", _entry_complete())  # what the pump did
    snapshots = flush("t1")  # queue is empty by now

    assert len(snapshots) == 1
    assert snapshots[0].content["usageByRun"]["r1"]["totalTokens"] == 13


def test_flush_returns_nothing_for_a_thread_with_no_activity() -> None:
    eq = _queue()
    registry = ActivityRegistry()
    merged: asyncio.Queue = asyncio.Queue()
    _pump, flush = _make_activity_pump(eq, registry, "main", merged, entry_inline=False)

    assert flush("t1") == []
