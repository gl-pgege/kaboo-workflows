"""Pausing a run that has filled its response, so it continues in the next one.

Three seams: the budget counts what the SSE writer sends, the hook reads it at
a tool boundary and interrupts there, and the AG-UI mapper marks that interrupt
as a continuation so the client resumes it without asking anyone.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from ag_ui.core.types import Interrupt
from strands.hooks.events import BeforeToolCallEvent

from kaboo_workflows._context import (
    ResponseBudget,
    get_response_budget,
    set_response_budget,
)
from kaboo_workflows.adapters.agui import (
    _DONE as _DONE_SENTINEL,
)
from kaboo_workflows.adapters.agui import (
    _map_strands_interrupt_to_agui,
    _resolve_turn_id,
    _sse_response,
)
from kaboo_workflows.hooks.continuation_hook import ContinuationHook


class _FakeStrandsInterrupt:
    def __init__(self, interrupt_id: str, reason: Any) -> None:
        self.id = interrupt_id
        self.reason = reason


class _RecordingEvent:
    """Stands in for BeforeToolCallEvent: records any interrupt raised."""

    def __init__(self, tool_name: str = "run_sql") -> None:
        self.tool_use = {"toolUseId": "use-1", "name": tool_name, "input": {}}
        self.cancel_tool: str | bool = False
        self.raised: list[dict[str, Any]] = []

    def interrupt(self, *, name: str, reason: dict[str, Any]) -> Any:
        self.raised.append({"name": name, "reason": reason})
        return None


def _fire(budget: ResponseBudget | None) -> _RecordingEvent:
    set_response_budget(budget)
    try:
        event = _RecordingEvent()
        ContinuationHook()._on_before_tool(cast(BeforeToolCallEvent, event))  # noqa: SLF001
        return event
    finally:
        set_response_budget(None)


def test_a_run_within_its_budget_is_not_paused() -> None:
    event = _fire(ResponseBudget(limit=1000, sent=999))
    assert event.raised == []


def test_a_run_that_has_spent_its_budget_pauses_before_the_tool() -> None:
    event = _fire(ResponseBudget(limit=1000, sent=1000))
    assert len(event.raised) == 1
    assert event.raised[0]["name"] == "kaboo:continuation"
    assert event.raised[0]["reason"]["type"] == "continuation"


def test_no_budget_means_no_ceiling() -> None:
    """The default: hosts that do not cap responses pay nothing for this."""
    assert _fire(None).raised == []
    assert _fire(ResponseBudget(limit=0, sent=10_000_000)).raised == []


def test_the_pause_lands_before_the_tool_runs() -> None:
    """Interrupting rather than cancelling is what makes the pause free.

    The tool is left untouched and unexecuted, so the resumed run performs it
    exactly once — as opposed to a cancelled call, which the model would see as
    a refusal and have to work around.
    """
    event = _fire(ResponseBudget(limit=1, sent=1))
    assert event.cancel_tool is False
    assert event.tool_use == {"toolUseId": "use-1", "name": "run_sql", "input": {}}


def test_the_next_response_starts_with_a_fresh_budget() -> None:
    """Why the hook needs no memory of having paused.

    Every request binds a new budget, so on the resume the same hook, on the
    same tool call, simply proceeds.
    """
    spent = ResponseBudget(limit=100, sent=100)
    assert _fire(spent).raised != []
    assert _fire(ResponseBudget(limit=100, sent=0)).raised == []


def test_the_mapper_marks_a_continuation_as_such() -> None:
    """The client must be able to tell this from a question for the user."""
    descriptor = _map_strands_interrupt_to_agui(
        _FakeStrandsInterrupt(
            "v1:before_tool_call:use-1:abc",
            {"type": "continuation", "message": "Continuing in a new response"},
        )
    )
    assert descriptor["reason"] == "continuation"
    assert descriptor["metadata"]["type"] == "continuation"
    # It still has to be a valid AG-UI interrupt: it travels the ordinary
    # RUN_FINISHED outcome, and the client resumes it the ordinary way.
    parsed = Interrupt.model_validate(descriptor)
    assert parsed.reason == "continuation"
    assert parsed.tool_call_id == "use-1"


def test_an_approval_is_still_an_approval() -> None:
    descriptor = _map_strands_interrupt_to_agui(
        _FakeStrandsInterrupt(
            "v1:before_tool_call:use-1:abc",
            {"type": "approval", "message": "m", "tool_name": "t", "tool_input": {}},
        )
    )
    assert descriptor["reason"] == "tool_call"


def test_a_continued_turn_keeps_its_turn_id() -> None:
    """Why the split is invisible: both responses belong to one turn.

    The resume POST carries a fresh ``run_id``, so only the turn id can hold
    the two halves together — it is what the UI binds a turn's cards to.
    """
    first = _resolve_turn_id("t1", "run-1", is_resume=False)
    continued = _resolve_turn_id("t1", "run-2", is_resume=True)

    assert continued == first

    # A genuinely new user message still starts a new turn.
    assert _resolve_turn_id("t1", "run-3", is_resume=False) != first


class _Event:
    """Minimal AG-UI-shaped event; the encoder stub only needs a size."""

    def __init__(self, size: int) -> None:
        self.type = "TEXT_MESSAGE_CONTENT"
        self.size = size


class _Encoder:
    def encode(self, event: Any) -> str:
        return "x" * event.size

    def get_content_type(self) -> str:
        return "text/event-stream"


async def _drain(events: list[Any], budget: ResponseBudget) -> list[str]:
    merged: asyncio.Queue[Any] = asyncio.Queue()

    async def consume() -> None:
        for event in events:
            await merged.put(event)
        await merged.put(_DONE_SENTINEL)

    async def pump() -> None:
        await asyncio.sleep(3600)

    return [
        frame
        async for frame in _sse_response(
            consume,
            pump,
            cast(Any, _Encoder()),
            merged,
            budget=budget,
        )
    ]


async def test_the_writer_counts_every_frame_it_sends() -> None:
    budget = ResponseBudget(limit=0)
    frames = await _drain([_Event(10), _Event(25), _Event(5)], budget)

    assert [len(f) for f in frames] == [10, 25, 5]
    assert budget.sent == 40


async def test_the_count_is_bytes_not_characters() -> None:
    """A budget measured against a socket limit has to agree with the socket."""
    budget = ResponseBudget(limit=0)
    merged: asyncio.Queue[Any] = asyncio.Queue()

    class _Unicode:
        def encode(self, event: Any) -> str:
            return "é" * event.size

        def get_content_type(self) -> str:
            return "text/event-stream"

    async def consume() -> None:
        await merged.put(_Event(4))
        await merged.put(_DONE_SENTINEL)

    async def pump() -> None:
        await asyncio.sleep(3600)

    frames = [
        frame
        async for frame in _sse_response(
            consume, pump, cast(Any, _Unicode()), merged, budget=budget
        )
    ]

    assert len(frames[0]) == 4
    assert budget.sent == 8


async def test_the_writer_and_the_hook_share_one_counter() -> None:
    """The whole mechanism rests on this: they run in different tasks."""
    budget = ResponseBudget(limit=30)
    set_response_budget(budget)
    try:
        seen: list[bool] = []

        async def consume() -> None:
            merged.put_nowait(_Event(20))
            await asyncio.sleep(0)
            # Stands in for a hook firing on the run task mid-response.
            observed = get_response_budget()
            seen.append(observed is not None and observed.exhausted)
            merged.put_nowait(_Event(20))
            await asyncio.sleep(0)
            observed = get_response_budget()
            seen.append(observed is not None and observed.exhausted)
            merged.put_nowait(_DONE_SENTINEL)

        async def pump() -> None:
            await asyncio.sleep(3600)

        merged: asyncio.Queue[Any] = asyncio.Queue()
        async for _ in _sse_response(consume, pump, cast(Any, _Encoder()), merged, budget=budget):
            pass

        assert seen == [False, True]
    finally:
        set_response_budget(None)
