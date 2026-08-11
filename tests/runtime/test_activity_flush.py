"""The activity pump's flush drains queued events at stream end.

The entry agent's final ``AGENT_COMPLETE`` (carrying the run's token usage)
lands on the event queue just before ``RUN_FINISHED``; without a synchronous
drain, the pump task can lose it to the stream-shutdown race and the final
``ACTIVITY_SNAPSHOT`` never reaches the client.
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


def test_flush_folds_queued_entry_usage_into_a_snapshot() -> None:
    eq = _queue()
    registry = ActivityRegistry()
    merged: asyncio.Queue = asyncio.Queue()
    _pump, flush = _make_activity_pump(eq, registry, "main", merged, entry_inline=False)

    eq.put_event(_entry_complete())
    snapshots = flush()

    assert len(snapshots) == 1
    content = snapshots[0].content
    assert content["usageByRun"]["r1"]["totalTokens"] == 13
    assert "main" not in content["groups"]  # entry stream stays unrendered


def test_flush_on_empty_queue_returns_nothing() -> None:
    eq = _queue()
    registry = ActivityRegistry()
    merged: asyncio.Queue = asyncio.Queue()
    _pump, flush = _make_activity_pump(eq, registry, "main", merged, entry_inline=False)

    assert flush() == []
