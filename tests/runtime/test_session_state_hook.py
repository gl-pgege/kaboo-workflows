"""SessionStateHook: interrupt state carried on the AG-UI state channel."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from strands.interrupt import Interrupt, PendingToolExecution, _InterruptState

from kaboo_workflows._context import HistoryExchange, SessionExchange, set_session_exchange
from kaboo_workflows.adapters._sessions import SESSIONS_KEY, inbound_sessions, sessions_delta
from kaboo_workflows.hooks import SessionStateHook, restore_session_state, session_state_snapshot


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    set_session_exchange(None)
    yield
    set_session_exchange(None)


def _paused_state() -> dict[str, Any]:
    state = _InterruptState()
    state.interrupts["int-1"] = Interrupt(id="int-1", name="approval", reason="approve the wire")
    state.activate()
    return state.to_dict()


def _agent(state: _InterruptState | None = None) -> SimpleNamespace:
    return SimpleNamespace(_interrupt_state=state if state is not None else _InterruptState())


def _event(agent: SimpleNamespace) -> Any:
    """Stub BeforeInvocationEvent (only ``.agent`` is read)."""
    return SimpleNamespace(agent=agent)


def test_restores_a_pending_interrupt_onto_a_fresh_agent():
    paused = _paused_state()
    set_session_exchange(SessionExchange(inbound={"interrupt_state": paused}, resuming=True))
    agent = _agent()

    SessionStateHook()._on_before_invocation(_event(agent))

    assert agent._interrupt_state.activated is True
    assert "int-1" in agent._interrupt_state.interrupts
    assert agent._interrupt_state.interrupts["int-1"].reason == "approve the wire"


def test_a_fresh_run_leaves_the_carried_gate_behind_and_clears_it():
    """A new message supersedes the gate, and a replayed one may be answered already."""
    exchange = SessionExchange(inbound={"interrupt_state": _paused_state()})
    set_session_exchange(exchange)
    agent = _agent()

    SessionStateHook()._on_before_invocation(_event(agent))

    assert agent._interrupt_state.activated is False
    assert exchange.agent is agent
    snapshot = session_state_snapshot()
    assert snapshot is not None
    assert snapshot["interrupt_state"]["activated"] is False


def test_records_the_agent_so_the_snapshot_can_read_it():
    set_session_exchange(SessionExchange())
    agent = _agent()
    SessionStateHook()._on_before_invocation(_event(agent))

    state = _InterruptState()
    state.interrupts["int-2"] = Interrupt(id="int-2", name="approval", reason="later")
    state.activate()
    agent._interrupt_state = state

    # Serialized at snapshot time, so a gate raised mid-run is still captured.
    assert session_state_snapshot() == {"interrupt_state": state.to_dict()}


def test_leaves_a_warm_agents_own_state_alone():
    """A stale client snapshot must not resurrect a gate the agent has answered."""
    live = _InterruptState()
    live.interrupts["fresh"] = Interrupt(id="fresh", name="approval", reason="current")
    live.activate()
    agent = _agent(live)
    set_session_exchange(
        SessionExchange(inbound={"interrupt_state": _paused_state()}, resuming=True)
    )

    SessionStateHook()._on_before_invocation(_event(agent))

    assert list(agent._interrupt_state.interrupts) == ["fresh"]


@pytest.mark.parametrize(
    "inbound",
    [
        {},
        {"interrupt_state": None},
        {"interrupt_state": {"activated": True}},  # partial: from_dict would KeyError
        {"interrupt_state": "nonsense"},
    ],
)
def test_tolerates_missing_or_malformed_state(inbound: dict[str, Any]):
    set_session_exchange(SessionExchange(inbound=inbound, resuming=True))
    agent = _agent()

    SessionStateHook()._on_before_invocation(_event(agent))

    assert agent._interrupt_state.activated is False


def test_no_snapshot_key_for_a_run_that_never_pauses():
    set_session_exchange(SessionExchange())
    SessionStateHook()._on_before_invocation(_event(_agent()))
    assert session_state_snapshot() is None


def _paused_with_pending_tools() -> dict[str, Any]:
    state = _InterruptState()
    for index in range(2):
        state.interrupts[f"int-{index}"] = Interrupt(
            id=f"int-{index}", name="approval", reason=f"save picture {index}"
        )
    state.pending_tool_execution = PendingToolExecution(
        assistant_message={
            "role": "assistant",
            "content": [
                {"toolUse": {"toolUseId": f"tool-{index}", "name": "save", "input": {}}}
                for index in range(2)
            ],
        },
        completed_tool_results=[],
    )
    state.activate()
    return state.to_dict()


def test_a_later_invocation_in_the_run_does_not_bring_back_the_answered_gate():
    """A hook that sends the agent back starts another invocation after the resumed tools ran."""
    set_session_exchange(
        SessionExchange(inbound={"interrupt_state": _paused_with_pending_tools()}, resuming=True)
    )
    agent = _agent()
    hook = SessionStateHook()

    hook._on_before_invocation(_event(agent))
    assert agent._interrupt_state.pending_tool_execution is not None
    agent._interrupt_state.end_tool_cycle()

    hook._on_before_invocation(_event(agent))

    assert agent._interrupt_state.activated is False
    assert agent._interrupt_state.interrupts == {}
    assert agent._interrupt_state.pending_tool_execution is None


def test_the_adapter_and_the_hook_restore_once_between_them():
    set_session_exchange(
        SessionExchange(inbound={"interrupt_state": _paused_state()}, resuming=True)
    )
    agent = _agent()

    assert restore_session_state(agent) is True
    assert restore_session_state(agent) is False
    agent._interrupt_state.deactivate()
    assert restore_session_state(agent) is False
    assert agent._interrupt_state.interrupts == {}


def test_clears_a_resolved_gate_rather_than_replaying_it():
    """Once resumed, the empty state must go out, or the next turn re-pauses."""
    set_session_exchange(
        SessionExchange(inbound={"interrupt_state": _paused_state()}, resuming=True)
    )
    agent = _agent()
    SessionStateHook()._on_before_invocation(_event(agent))
    agent._interrupt_state.deactivate()

    snapshot = session_state_snapshot()

    assert snapshot is not None
    assert snapshot["interrupt_state"]["activated"] is False
    assert snapshot["interrupt_state"]["interrupts"] == {}


def test_no_exchange_is_a_no_op():
    agent = _agent()
    SessionStateHook()._on_before_invocation(_event(agent))
    assert session_state_snapshot() is None


# ── the adapter's half of the round trip: kaboo_sessions ──────────────────────


def _written_back(exchange: HistoryExchange | None = None) -> dict[str, Any] | None:
    delta = sessions_delta(exchange or HistoryExchange())
    if delta is None:
        return None
    [operation] = delta.model_dump(by_alias=True)["delta"]
    assert (operation["op"], operation["path"]) == ("add", f"/{SESSIONS_KEY}")
    return operation["value"]


def test_the_run_writes_back_the_pending_gate_and_what_it_asks():
    set_session_exchange(SessionExchange())
    agent = _agent()
    SessionStateHook()._on_before_invocation(_event(agent))
    agent._interrupt_state.interrupts["int-3"] = Interrupt(
        id="int-3", name="approval", reason={"type": "approval", "tool_name": "submit_report"}
    )
    agent._interrupt_state.activate()

    sessions = _written_back()

    assert sessions is not None
    assert sessions["paused"]["interrupt_state"]["activated"] is True
    assert [a["interruptId"] for a in sessions["approvals"]] == ["int-3"]


def test_the_run_writes_back_every_agents_transcript():
    exchange = HistoryExchange(
        inbound={"team": [{"role": "user", "content": [{"text": "old"}]}]},
        outbound={"team": [{"role": "user", "content": [{"text": "new"}]}]},
    )
    set_session_exchange(SessionExchange())

    sessions = _written_back(exchange)

    assert sessions == {"history": {"team": [{"role": "user", "content": [{"text": "new"}]}]}}


def test_nothing_is_written_when_the_run_holds_and_carried_nothing():
    set_session_exchange(SessionExchange())
    SessionStateHook()._on_before_invocation(_event(_agent()))
    assert _written_back() is None


def test_a_resolved_gate_is_cleared_rather_than_replayed():
    set_session_exchange(SessionExchange(inbound={"interrupt_state": _paused_state()}))
    SessionStateHook()._on_before_invocation(_event(_agent()))

    sessions = _written_back()

    assert sessions is not None
    assert sessions["paused"]["interrupt_state"]["activated"] is False


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, ({}, {})),
        ({}, ({}, {})),
        ("nonsense", ({}, {})),
        ({SESSIONS_KEY: "nonsense"}, ({}, {})),
        ({SESSIONS_KEY: {"history": "x", "paused": []}}, ({}, {})),
        (
            {
                SESSIONS_KEY: {
                    "history": {"team": []},
                    "paused": {"interrupt_state": {"activated": True}},
                }
            },
            ({"team": []}, {"interrupt_state": {"activated": True}}),
        ),
    ],
)
def test_reads_client_state_defensively(
    state: Any, expected: tuple[dict[str, Any], dict[str, Any]]
):
    assert inbound_sessions(state) == expected
