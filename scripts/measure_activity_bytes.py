"""Measure what the activity channel costs on the wire, snapshots vs deltas.

Runs the real pump and registry over a synthetic event stream shaped like a
production run, and reports the bytes each mode would put on the SSE stream.

A deployed run is measured with ``scripts/testing/measure-run-stream.ts`` in the
kaboo repository; this isolates the activity channel so a change can be sized
without a deploy.

    uv run python scripts/measure_activity_bytes.py
"""

from __future__ import annotations

import asyncio
import json
import sys

from ag_ui.core import EventType as AGUIEventType

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters.agui import _make_activity_pump
from kaboo_workflows.types import EventType, StreamEvent
from kaboo_workflows.wire import EventQueue

THREAD = "t1"


def events(*, agents: int, tools_each: int, chunks_each: int) -> list[StreamEvent]:
    """A run of *agents* delegates, each calling tools and streaming prose."""
    out: list[StreamEvent] = []
    for a in range(agents):
        group = f"delegate-{a}"
        out.append(
            StreamEvent(
                type=EventType.STREAM_GROUP_START,
                agent_name=group,
                data={
                    "stream_group": group,
                    "stream_title": f"Delegate {a}",
                    "agent_name": group,
                    "thread_id": THREAD,
                    "task": "Analyse the quarter and report back",
                },
            )
        )
        for t in range(tools_each):
            tool_use_id = f"{group}-tc-{t}"
            out.append(
                StreamEvent(
                    type=EventType.TOOL_START,
                    agent_name=group,
                    data={
                        "stream_group": group,
                        "thread_id": THREAD,
                        "tool_use_id": tool_use_id,
                        "tool_name": "connector_get_data",
                        "tool_input": {"connectorId": f"c-{t}", "filters": {"q": "x" * 80}},
                    },
                )
            )
            out.append(
                StreamEvent(
                    type=EventType.TOOL_END,
                    agent_name=group,
                    data={
                        "stream_group": group,
                        "thread_id": THREAD,
                        "tool_use_id": tool_use_id,
                        "status": "done",
                        # Tool results are the bulk of a real tree.
                        "tool_result": json.dumps([{"row": i, "v": "y" * 40} for i in range(30)]),
                    },
                )
            )
            for c in range(chunks_each):
                out.append(
                    StreamEvent(
                        type=EventType.TOKEN,
                        agent_name=group,
                        data={
                            "stream_group": group,
                            "thread_id": THREAD,
                            "text": f"token {c} of some streamed prose. ",
                        },
                    )
                )
        out.append(
            StreamEvent(
                type=EventType.STREAM_GROUP_END,
                agent_name=group,
                data={"stream_group": group, "thread_id": THREAD, "status": "completed"},
            )
        )
    return out


async def measure(stream: list[StreamEvent], *, deltas: bool, burst: int) -> tuple[int, int]:
    """Bytes the activity channel emits, and how many events it takes.

    *burst* is how many queued events a pump wakeup folds together, which is
    what coalescing exploits: a real pump drains whatever arrived in its 0.15s
    tick, so a busy run folds many events per snapshot.
    """
    eq = EventQueue(asyncio.Queue(maxsize=100_000), entry_name="main")
    merged: asyncio.Queue = asyncio.Queue()
    registry = ActivityRegistry()
    pump, flush = _make_activity_pump(
        eq, registry, "main", merged, entry_inline=False, deltas=deltas
    )

    task = asyncio.create_task(pump())
    for i in range(0, len(stream), burst):
        for event in stream[i : i + burst]:
            eq.put_event(event)
        await asyncio.sleep(0.16)
    await eq.close()
    try:
        await asyncio.wait_for(task, timeout=5)
    except (TimeoutError, asyncio.TimeoutError):
        task.cancel()

    emitted = [merged.get_nowait() for _ in range(merged.qsize())]
    emitted += flush(THREAD)

    total = 0
    for event in emitted:
        if event.type == AGUIEventType.ACTIVITY_SNAPSHOT:
            total += len(json.dumps(event.content))
        else:
            total += len(json.dumps(event.patch))
    return total, len(emitted)


def mb(n: int) -> str:
    return f"{n / 1_048_576:.2f} MB"


async def main() -> None:
    shapes = {
        "single agent, 7 tools": dict(agents=1, tools_each=7, chunks_each=20),
        "5 delegates, 6 tools each": dict(agents=5, tools_each=6, chunks_each=20),
        "10 delegates, 10 tools each": dict(agents=10, tools_each=10, chunks_each=20),
    }
    print(f"{'SHAPE':<30}{'SNAPSHOTS':>14}{'DELTAS':>14}{'SAVING':>10}")
    for label, shape in shapes.items():
        stream = events(**shape)  # type: ignore[arg-type]
        snap, snap_n = await measure(stream, deltas=False, burst=8)
        delta, delta_n = await measure(stream, deltas=True, burst=8)
        saving = f"{snap / max(delta, 1):.0f}x"
        print(f"{label:<30}{mb(snap):>14}{mb(delta):>14}{saving:>10}")
        print(f"{'':<30}{f'{snap_n} events':>14}{f'{delta_n} events':>14}")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
