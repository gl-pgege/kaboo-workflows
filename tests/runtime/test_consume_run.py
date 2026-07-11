"""Tool-call lifecycle in ``_consume_run`` — exactly one terminal result.

The follow-up hang was a *duplicate* ``toolResult``: a paused-for-interrupt call
was closed with an empty result, then emitted its real result on resume. These
tests script an ag-ui-strands-style event stream and assert the emission
contract: a paused call is left open, a genuinely-finished or errored call is
closed once, and a superseded (abandoned) call is backfilled after RUN_STARTED.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from ag_ui.core import (
    EventType,
    RunFinishedEvent,
    RunStartedEvent,
    ToolCallStartEvent,
)

from kaboo_workflows._context import HistoryExchange
from kaboo_workflows.adapters import agui as agui_mod
from kaboo_workflows.adapters.agui import _DONE, _consume_run


class _FakeAguiAgent:
    def __init__(self, events: list, *, raise_after: int | None = None) -> None:
        self._events = events
        self._raise_after = raise_after

    def run(self, _input_data: object) -> AsyncIterator:
        events, raise_after = self._events, self._raise_after

        async def _gen() -> AsyncIterator:
            for idx, event in enumerate(events):
                yield event
                if raise_after is not None and idx == raise_after:
                    raise RuntimeError("model blew up")

        return _gen()


def _started() -> RunStartedEvent:
    return RunStartedEvent(type=EventType.RUN_STARTED, thread_id="t", run_id="r")


def _tool_start(tc_id: str) -> ToolCallStartEvent:
    return ToolCallStartEvent(
        type=EventType.TOOL_CALL_START, tool_call_id=tc_id, tool_call_name="research_team"
    )


def _finished() -> RunFinishedEvent:
    return RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id="t", run_id="r")


@pytest.fixture
def _patch_bridge(monkeypatch: pytest.MonkeyPatch):
    def _apply(*, interrupt_active: bool) -> None:
        monkeypatch.setattr(agui_mod.bridge, "get_thread_agent", lambda a, t: object())
        monkeypatch.setattr(agui_mod.bridge, "is_interrupt_active", lambda s: interrupt_active)
        pending = (
            {"i1": SimpleNamespace(id="i1", reason={"type": "approval", "message": "ok"})}
            if interrupt_active
            else {}
        )
        monkeypatch.setattr(
            agui_mod.bridge, "pending_interrupts", lambda s, unresolved_only=True: pending
        )

    return _apply


async def _drive(agent: _FakeAguiAgent, *, backfill: list[str] | None = None) -> list:
    merged: asyncio.Queue = asyncio.Queue()
    input_data = SimpleNamespace(thread_id="t", run_id="r")
    await _consume_run(
        cast(Any, agent),
        cast(Any, input_data),
        merged,
        HistoryExchange(),
        backfill_tool_calls=backfill,
    )
    items: list = []
    while not merged.empty():
        items.append(merged.get_nowait())
    return items


def _results_for(items: list, tc_id: str) -> list:
    return [
        it
        for it in items
        if getattr(it, "type", None) == EventType.TOOL_CALL_RESULT
        and getattr(it, "tool_call_id", None) == tc_id
    ]


async def test_paused_call_is_not_closed(_patch_bridge) -> None:
    _patch_bridge(interrupt_active=True)
    agent = _FakeAguiAgent([_started(), _tool_start("tc1"), _finished()])

    items = await _drive(agent)

    # The open call is left dangling on pause — no premature empty result.
    assert _results_for(items, "tc1") == []
    assert items[-1] is _DONE
    finished = next(it for it in items if getattr(it, "type", None) == EventType.RUN_FINISHED)
    assert getattr(finished.outcome, "type", None) == "interrupt"


async def test_finished_run_closes_open_call_once(_patch_bridge) -> None:
    _patch_bridge(interrupt_active=False)
    agent = _FakeAguiAgent([_started(), _tool_start("tc1"), _finished()])

    items = await _drive(agent)

    # A truly-finished run closes anything strands left open — exactly once.
    assert len(_results_for(items, "tc1")) == 1


async def test_errored_run_closes_open_call_then_errors(_patch_bridge) -> None:
    _patch_bridge(interrupt_active=False)
    agent = _FakeAguiAgent([_started(), _tool_start("tc1")], raise_after=1)

    items = await _drive(agent)

    assert len(_results_for(items, "tc1")) == 1
    assert any(getattr(it, "type", None) == EventType.RUN_ERROR for it in items)


async def test_superseded_call_is_backfilled_after_run_started(_patch_bridge) -> None:
    _patch_bridge(interrupt_active=False)
    agent = _FakeAguiAgent([_started(), _finished()])

    items = await _drive(agent, backfill=["abandoned1"])

    backfilled = _results_for(items, "abandoned1")
    assert len(backfilled) == 1
    # Ordered after RUN_STARTED to keep the AG-UI protocol valid.
    started_idx = next(
        i for i, it in enumerate(items) if getattr(it, "type", None) == EventType.RUN_STARTED
    )
    result_idx = items.index(backfilled[0])
    assert result_idx > started_idx
