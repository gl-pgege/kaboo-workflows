"""StrandsMultiAgent run loop over real Swarm/Graph orchestrations.

Drives ``StrandsMultiAgent.consume`` against genuine strands ``Swarm``/``Graph``
objects built from owned fake models (no network), asserting the AG-UI contract:
one RUN_STARTED, the chat-output node's text as TEXT_MESSAGE_*, a terminal
RUN_FINISHED, interrupt-outcome mapping, and the result-text fallback when
nothing streamed live.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

from ag_ui.core import EventType, RunAgentInput
from strands import Agent
from strands.interrupt import Interrupt
from strands.multiagent import GraphBuilder, Swarm
from strands.multiagent.base import MultiAgentBase
from strands.multiagent.graph import Graph

from kaboo_workflows._context import HistoryExchange
from kaboo_workflows.adapters import _multiagent as ma
from kaboo_workflows.adapters._multiagent import _DONE, StrandsMultiAgent
from tests.fakes.strands import FakeModel


def _agent(name: str, chunks: list[str]) -> Agent:
    return Agent(name=name, model=FakeModel(chunks), system_prompt="x")


def _graph(chat_terminal_chunks: list[str]) -> Graph:
    a = _agent("a", ["draft"])
    b = _agent("b", chat_terminal_chunks)
    builder = GraphBuilder()
    builder.add_node(a, "a")
    builder.add_node(b, "b")
    builder.add_edge("a", "b")
    builder.set_entry_point("a")
    return builder.build()


def _swarm(chunks: list[str]) -> Swarm:
    writer = _agent("writer", chunks)
    return Swarm(id="team", nodes=[writer], entry_point=writer, max_handoffs=2)


def _input(**overrides) -> SimpleNamespace:
    defaults = dict(
        thread_id="t1",
        run_id="r1",
        messages=[SimpleNamespace(role="user", content="do it")],
        state=None,
        resume=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def _drive(runner: StrandsMultiAgent, input_data, *, resume_entries=None) -> list:
    merged: asyncio.Queue = asyncio.Queue()
    await runner.consume(input_data, merged, HistoryExchange(), resume_entries=resume_entries)
    items: list = []
    while not merged.empty():
        items.append(merged.get_nowait())
    return items


def _types(items: list) -> list:
    return [getattr(it, "type", it) for it in items]


def _text(items: list) -> str:
    return "".join(
        it.delta for it in items if getattr(it, "type", None) == EventType.TEXT_MESSAGE_CONTENT
    )


async def test_graph_streams_chat_output_node_text_live():
    runner = StrandsMultiAgent(_graph(["Edited ", "copy."]), name="pipe", chat_output="b")

    items = await _drive(runner, _input())

    types = _types(items)
    assert types[0] == EventType.RUN_STARTED
    assert types[-2] if types[-1] is _DONE else types[-1]  # sanity
    assert items[-1] is _DONE
    assert EventType.TEXT_MESSAGE_START in types
    assert EventType.TEXT_MESSAGE_END in types
    # Only the chat-output node ("b") is voiced; node "a" text is not in the bubble.
    assert _text(items) == "Edited copy."
    finished = next(it for it in items if getattr(it, "type", None) == EventType.RUN_FINISHED)
    assert getattr(finished, "outcome", None) is None


async def test_swarm_streams_chat_output_node_text():
    runner = StrandsMultiAgent(_swarm(["Final ", "report."]), name="team", chat_output="writer")

    items = await _drive(runner, _input())

    assert _text(items) == "Final report."
    assert items[-1] is _DONE


async def test_no_chat_output_falls_back_to_final_node_text_at_end():
    # chat_output=None: nothing streams live, but the run must still produce a
    # reply — the last-completed node's text, emitted as one message.
    runner = StrandsMultiAgent(_graph(["Edited ", "copy."]), name="pipe", chat_output=None)

    items = await _drive(runner, _input())

    assert _text(items) == "Edited copy."
    # Exactly one text message bracket.
    types = _types(items)
    assert types.count(EventType.TEXT_MESSAGE_START) == 1
    assert types.count(EventType.TEXT_MESSAGE_END) == 1


async def test_interrupt_outcome_is_emitted_when_orchestrator_paused(monkeypatch):
    runner = StrandsMultiAgent(_swarm(["Final report."]), name="team", chat_output="writer")

    # Simulate the orchestrator pausing on an interrupt after the run.
    monkeypatch.setattr(ma.bridge, "is_interrupt_active", lambda o: True)
    monkeypatch.setattr(
        ma.bridge,
        "pending_interrupts",
        lambda o, unresolved_only=True: {
            "i1": SimpleNamespace(id="i1", reason={"type": "approval", "message": "ok?"})
        },
    )

    items = await _drive(runner, _input())

    finished = next(it for it in items if getattr(it, "type", None) == EventType.RUN_FINISHED)
    outcome = getattr(finished, "outcome", None)
    assert outcome is not None and getattr(outcome, "type", None) == "interrupt"
    interrupts = outcome.interrupts
    assert getattr(interrupts[0], "id", None) == "i1"
    assert getattr(interrupts[0], "reason", None) == "tool_call"


async def test_run_error_is_emitted_when_stream_raises(monkeypatch):
    runner = StrandsMultiAgent(_swarm(["hi"]), name="team", chat_output="writer")

    async def _boom(_task):
        raise RuntimeError("kaboom")
        yield  # pragma: no cover

    monkeypatch.setattr(runner.orchestrator, "stream_async", _boom)

    items = await _drive(runner, _input())

    assert any(getattr(it, "type", None) == EventType.RUN_ERROR for it in items)
    assert items[-1] is _DONE


async def test_same_instance_runs_are_independent():
    runner = StrandsMultiAgent(_graph(["Edited ", "copy."]), name="pipe", chat_output="b")

    first = await _drive(runner, _input(run_id="r1"))
    second = await _drive(runner, _input(run_id="r2"))

    assert _text(first) == "Edited copy."
    assert _text(second) == "Edited copy."


# -- per-thread interrupt isolation ------------------------------------------


GATE_ID = "v1:before_tool_call:tc-9:u1"
GATE_REASON = {"type": "approval", "message": "ok?", "expiresAt": "2099-01-01T00:00:00Z"}


def _pausing_stream(runner: StrandsMultiAgent, interrupt_id: str = GATE_ID):
    """A stream that raises a tool gate on the orchestrator's live state."""

    async def stream(_task):
        istate = ma.bridge.get_interrupt_state(runner.orchestrator)
        assert istate is not None
        istate.interrupts[interrupt_id] = Interrupt(
            id=interrupt_id, name="gate", reason=dict(GATE_REASON)
        )
        istate.activate()
        yield {"type": "noop"}

    return stream


async def test_paused_gate_is_parked_per_thread(monkeypatch):
    runner = StrandsMultiAgent(_swarm(["hi"]), name="team", chat_output="writer")
    monkeypatch.setattr(runner.orchestrator, "stream_async", _pausing_stream(runner))

    items = await _drive(runner, _input(thread_id="t1"))

    finished = next(it for it in items if getattr(it, "type", None) == EventType.RUN_FINISHED)
    descriptor = finished.outcome.interrupts[0]
    assert descriptor.id == GATE_ID
    assert descriptor.tool_call_id == "tc-9"
    assert descriptor.expires_at == "2099-01-01T00:00:00Z"

    assert runner.is_interrupt_active("t1")
    assert GATE_ID in runner.pending_interrupts("t1")
    assert not runner.is_interrupt_active("t2")
    # Between runs the orchestrator's live state is inert: no other thread's
    # run can observe (or clobber) t1's gate.
    assert not ma.bridge.is_interrupt_active(runner.orchestrator)


async def test_other_thread_run_leaves_parked_gate_untouched(monkeypatch):
    runner = StrandsMultiAgent(_swarm(["hi"]), name="team", chat_output="writer")
    monkeypatch.setattr(runner.orchestrator, "stream_async", _pausing_stream(runner))
    await _drive(runner, _input(thread_id="t1"))

    async def clean_stream(_task):
        yield {"type": "multiagent_node_stream", "node_id": "writer", "event": {"data": "done"}}

    monkeypatch.setattr(runner.orchestrator, "stream_async", clean_stream)
    items = await _drive(runner, _input(thread_id="t2", run_id="r2"))

    finished = next(it for it in items if getattr(it, "type", None) == EventType.RUN_FINISHED)
    assert getattr(finished, "outcome", None) is None
    assert runner.is_interrupt_active("t1")
    assert not runner.is_interrupt_active("t2")


async def test_resume_restores_the_threads_parked_state(monkeypatch):
    runner = StrandsMultiAgent(_swarm(["hi"]), name="team", chat_output="writer")
    monkeypatch.setattr(runner.orchestrator, "stream_async", _pausing_stream(runner))
    await _drive(runner, _input(thread_id="t1"))

    seen: dict = {}

    async def resume_stream(task):
        istate = ma.bridge.get_interrupt_state(runner.orchestrator)
        assert istate is not None
        seen["task"] = task
        seen["was_active"] = istate.activated
        istate.deactivate()
        yield {"type": "multiagent_node_stream", "node_id": "writer", "event": {"data": "ok"}}

    monkeypatch.setattr(runner.orchestrator, "stream_async", resume_stream)
    await _drive(
        runner,
        _input(thread_id="t1", run_id="r2"),
        resume_entries=[
            {"interruptId": GATE_ID, "status": "resolved", "payload": {"status": "approved"}}
        ],
    )

    # The parked gate was live again for the resume, addressed by the entry,
    # and — once settled — no longer parked.
    assert seen["was_active"] is True
    assert seen["task"] == [
        {"interruptResponse": {"interruptId": GATE_ID, "response": {"status": "approved"}}}
    ]
    assert not runner.is_interrupt_active("t1")


async def test_deactivate_clears_only_the_target_thread(monkeypatch):
    runner = StrandsMultiAgent(_swarm(["hi"]), name="team", chat_output="writer")
    monkeypatch.setattr(
        runner.orchestrator, "stream_async", _pausing_stream(runner, "v1:tool_call:tc-1:a")
    )
    await _drive(runner, _input(thread_id="t1"))
    monkeypatch.setattr(
        runner.orchestrator, "stream_async", _pausing_stream(runner, "v1:tool_call:tc-2:b")
    )
    await _drive(runner, _input(thread_id="t2", run_id="r2"))

    runner.deactivate_interrupts("t1")

    assert not runner.is_interrupt_active("t1")
    assert runner.is_interrupt_active("t2")
    assert "v1:tool_call:tc-2:b" in runner.pending_interrupts("t2")


def test_multiagent_uses_the_shared_interrupt_mapper():
    from kaboo_workflows.adapters._interrupts import map_strands_interrupt_to_agui

    assert ma._map_strands_interrupt_to_agui is map_strands_interrupt_to_agui


# -- pure helpers -------------------------------------------------------------


def test_extract_user_task_returns_latest_user_message():
    input_data = _input(
        messages=[
            SimpleNamespace(role="user", content="first"),
            SimpleNamespace(role="assistant", content="reply"),
            SimpleNamespace(role="user", content="second"),
        ]
    )
    assert ma._extract_user_task(cast(RunAgentInput, input_data)) == "second"


def test_build_resume_responses_maps_pending_interrupts(monkeypatch):
    orchestrator = object()
    monkeypatch.setattr(
        ma.bridge, "pending_interrupts", lambda o, unresolved_only=True: {"i1": object()}
    )
    responses = ma._build_resume_responses(
        cast(MultiAgentBase, orchestrator),
        [{"interruptId": "i1", "status": "resolved", "payload": {"ok": True}}],
    )
    assert responses == [{"interruptResponse": {"interruptId": "i1", "response": {"ok": True}}}]


def test_build_resume_responses_defaults_unaddressed_to_cancelled(monkeypatch):
    monkeypatch.setattr(
        ma.bridge, "pending_interrupts", lambda o, unresolved_only=True: {"i1": object()}
    )
    responses = ma._build_resume_responses(cast(MultiAgentBase, object()), [])
    assert responses[0]["interruptResponse"]["response"] == {"status": "cancelled"}


def test_result_text_extracts_node_agent_text():
    message = {"role": "assistant", "content": [{"text": "hello "}, {"text": "world"}]}
    agent_result = SimpleNamespace(message=message)
    node_result = SimpleNamespace(get_agent_results=lambda: [agent_result])
    result = SimpleNamespace(results={"b": node_result})
    assert ma._result_text(result, "b") == "hello world"
