"""Client-driven history hook — seed/capture behaviour on real strands agents.

``HistoryHook`` is the mechanism that makes a sub-agent's transcript live in the
AG-UI ``state`` blob rather than a process singleton: it seeds ``agent.messages``
before an invocation and captures them after. We drive it with a real ``Agent``
and real ``BeforeInvocationEvent`` / ``AfterInvocationEvent`` (no MagicMock) and
assert the observable contract.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from strands import Agent
from strands.hooks import HookRegistry
from strands.hooks.events import AfterInvocationEvent, BeforeInvocationEvent

from kaboo_workflows._context import HistoryExchange, set_history_exchange
from kaboo_workflows.hooks import HistoryHook
from tests.fakes import FakeModel


def _msg(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


def _fire_before(hook: HistoryHook, agent: Agent) -> None:
    registry = HookRegistry()
    registry.add_hook(hook)
    registry.invoke_callbacks(BeforeInvocationEvent(agent=agent))


def _fire_after(hook: HistoryHook, agent: Agent) -> None:
    registry = HookRegistry()
    registry.add_hook(hook)
    registry.invoke_callbacks(AfterInvocationEvent(agent=agent))


@pytest.fixture(autouse=True)
def _isolate_exchange() -> Iterator[None]:
    set_history_exchange(None)
    yield
    set_history_exchange(None)


@pytest.fixture
def agent() -> Agent:
    return Agent(model=FakeModel())


def test_seeds_from_inbound_when_enabled(agent: Agent) -> None:
    set_history_exchange(HistoryExchange(inbound={"k": [_msg("prior")]}))
    agent.messages = [_msg("stale singleton")]

    _fire_before(HistoryHook("k", enabled=True), agent)

    assert agent.messages == [_msg("prior")]


def test_outbound_takes_precedence_over_inbound(agent: Agent) -> None:
    set_history_exchange(
        HistoryExchange(inbound={"k": [_msg("from client")]}, outbound={"k": [_msg("this run")]})
    )
    _fire_before(HistoryHook("k", enabled=True), agent)

    assert agent.messages == [_msg("this run")]


def test_seed_is_deep_copied_not_aliased(agent: Agent) -> None:
    exchange = HistoryExchange(inbound={"k": [_msg("prior")]})
    set_history_exchange(exchange)

    _fire_before(HistoryHook("k", enabled=True), agent)
    agent.messages[0]["content"][0]["text"] = "mutated"

    assert exchange.inbound["k"] == [_msg("prior")]


def test_disabled_resets_to_empty(agent: Agent) -> None:
    set_history_exchange(HistoryExchange(inbound={"k": [_msg("prior")]}))
    agent.messages = [_msg("stale singleton")]

    _fire_before(HistoryHook("k", enabled=False), agent)

    assert agent.messages == []


def test_no_exchange_is_a_noop(agent: Agent) -> None:
    agent.messages = [_msg("untouched")]

    _fire_before(HistoryHook("k", enabled=True), agent)

    assert agent.messages == [_msg("untouched")]


def test_active_interrupt_leaves_messages_intact(agent: Agent) -> None:
    set_history_exchange(HistoryExchange(inbound={"k": [_msg("prior")]}))
    agent.messages = [_msg("in-flight resume state")]
    agent._interrupt_state.activated = True

    _fire_before(HistoryHook("k", enabled=True), agent)

    assert agent.messages == [_msg("in-flight resume state")]


def test_captures_into_outbound_after_invocation(agent: Agent) -> None:
    exchange = HistoryExchange()
    set_history_exchange(exchange)
    agent.messages = [_msg("q"), _msg("a")]

    _fire_after(HistoryHook("k", enabled=True), agent)

    assert exchange.outbound["k"] == [_msg("q"), _msg("a")]


def test_disabled_does_not_capture(agent: Agent) -> None:
    exchange = HistoryExchange()
    set_history_exchange(exchange)
    agent.messages = [_msg("q")]

    _fire_after(HistoryHook("k", enabled=False), agent)

    assert "k" not in exchange.outbound


def test_round_trip_capture_then_reseed(agent: Agent) -> None:
    """Turn 1 captures the transcript; turn 2 re-seeds it from the same bucket."""
    exchange = HistoryExchange()
    set_history_exchange(exchange)
    hook = HistoryHook("k", enabled=True)

    agent.messages = [_msg("turn1 q"), _msg("turn1 a")]
    _fire_after(hook, agent)

    fresh = Agent(model=FakeModel())
    fresh.messages = [_msg("stale")]
    _fire_before(hook, fresh)

    assert fresh.messages == [_msg("turn1 q"), _msg("turn1 a")]
