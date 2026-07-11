"""History config resolution and wiring — keys, enablement, and chat-owner skip.

Covers the path from YAML-shaped ``AppConfig`` to the per-agent
``(key, enabled)`` map and the chat-owner that must never receive a
``HistoryHook``, then proves the wiring skip behaviourally on real agents.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from strands import Agent
from strands.hooks.events import BeforeInvocationEvent

from kaboo_workflows._context import HistoryExchange, set_history_exchange
from kaboo_workflows.config.resolvers.config import _resolve_chat_owner, _resolve_history
from kaboo_workflows.wire import make_event_queue
from tests.factories import agent_def, app_config, delegate_orchestration
from tests.fakes import FakeModel


def _msg(text: str) -> dict:
    return {"role": "user", "content": [{"text": text}]}


# ── _resolve_history ─────────────────────────────────────────────────────────


def test_history_defaults_to_disabled() -> None:
    cfg = app_config(agents={"a": agent_def(), "b": agent_def()}, entry="a")

    resolved = _resolve_history(cfg)

    assert resolved["a"] == ("a", False)
    assert resolved["b"] == ("b", False)


def test_global_history_default_enables_all() -> None:
    cfg = app_config(agents={"a": agent_def(), "b": agent_def()}, entry="a", history=True)

    resolved = _resolve_history(cfg)

    assert resolved["a"][1] is True
    assert resolved["b"][1] is True


def test_per_agent_override_beats_global_default() -> None:
    cfg = app_config(
        agents={"a": agent_def(), "b": agent_def(history=False)},
        entry="a",
        history=True,
    )

    resolved = _resolve_history(cfg)

    assert resolved["a"][1] is True
    assert resolved["b"][1] is False


def test_history_group_shares_one_key() -> None:
    cfg = app_config(
        agents={
            "researcher": agent_def(history={"enabled": True, "group": "team"}),
            "fact_checker": agent_def(history={"enabled": True, "group": "team"}),
        },
        entry="researcher",
    )

    resolved = _resolve_history(cfg)

    assert resolved["researcher"][0] == "team"
    assert resolved["fact_checker"][0] == "team"


# ── _resolve_chat_owner ──────────────────────────────────────────────────────


def test_chat_owner_is_delegate_entry_blueprint() -> None:
    cfg = app_config(
        agents={"coordinator": agent_def(), "worker": agent_def()},
        orchestrations={"pipeline": delegate_orchestration("coordinator", {"worker": "do work"})},
        entry="pipeline",
    )

    assert _resolve_chat_owner(cfg) == "coordinator"


def test_chat_owner_is_none_for_plain_agent_entry() -> None:
    cfg = app_config(agents={"a": agent_def()}, entry="a")

    assert _resolve_chat_owner(cfg) is None


# ── wiring: chat owner is skipped, others get a HistoryHook ───────────────────


@pytest.fixture(autouse=True)
def _isolate_exchange() -> Iterator[None]:
    set_history_exchange(None)
    yield
    set_history_exchange(None)


def test_chat_owner_gets_no_history_hook_but_others_do() -> None:
    coordinator = Agent(model=FakeModel(), name="coordinator")
    worker = Agent(model=FakeModel(), name="worker")
    agents = {"coordinator": coordinator, "worker": worker}

    make_event_queue(
        agents,
        history={"coordinator": ("coordinator", True), "worker": ("worker", True)},
        entry_name="pipeline",
        chat_owner="coordinator",
    )

    set_history_exchange(
        HistoryExchange(inbound={"coordinator": [_msg("c")], "worker": [_msg("w")]})
    )
    for ag in agents.values():
        ag.messages = [_msg("stale")]
        ag.hooks.invoke_callbacks(BeforeInvocationEvent(agent=ag))

    # Chat owner: no HistoryHook, so its transcript (the chat) is left untouched.
    assert coordinator.messages == [_msg("stale")]
    # Sub-agent: HistoryHook seeded it from the client-supplied bucket.
    assert worker.messages == [_msg("w")]
