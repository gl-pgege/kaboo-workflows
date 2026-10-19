"""Each conversation of a fixed-config process runs on its own session."""

from __future__ import annotations

from pathlib import Path

from ag_ui.core import RunAgentInput

from kaboo_workflows.adapters.agui import _ConversationSessions
from kaboo_workflows.config import parse_config_sources, resolve_infra, validate_raw_config

CONFIG = Path(__file__).resolve().parents[1] / "e2e" / "configs" / "delegate.yaml"


def _input(thread_id: str) -> RunAgentInput:
    return RunAgentInput(
        thread_id=thread_id,
        run_id="r1",
        state={},
        messages=[],
        tools=[],
        context=[],
        forwarded_props={},
    )


def _sessions(limit: int) -> _ConversationSessions:
    app_config = validate_raw_config(parse_config_sources(str(CONFIG)))
    return _ConversationSessions(app_config, resolve_infra(app_config), limit=limit)


def test_a_conversation_keeps_its_session_and_never_shares_one() -> None:
    sessions = _sessions(limit=8)
    first = sessions(_input("c1"))

    assert sessions(_input("c1")) is first
    other = sessions(_input("c2"))
    assert other is not first
    assert other.resolved.agents["worker_a"] is not first.resolved.agents["worker_a"]


def test_the_least_recently_used_conversation_is_dropped_past_the_limit() -> None:
    sessions = _sessions(limit=2)
    first = sessions(_input("c1"))
    second = sessions(_input("c2"))
    sessions(_input("c1"))
    sessions(_input("c3"))

    assert sessions(_input("c1")) is first
    assert sessions(_input("c2")) is not second
    assert len(sessions.kept()) == 2
