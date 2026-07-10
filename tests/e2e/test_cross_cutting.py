"""End-to-end coverage for cross-cutting behaviours across a run's lifecycle:

- error propagation (a failing node surfaces RUN_ERROR, never a hang),
- HITL interrupt → resume (ask_user pauses the run, resume continues it), and
- multi-turn client-driven history (a sub-agent's transcript round-trips through
  ``state.kaboo_history`` and accumulates across turns).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ag_ui.core import EventType as AGUIEventType

from .harness import Pipeline, run_pipeline

CONFIGS = Path(__file__).parent / "configs"


# --- error paths ----------------------------------------------------------


async def test_plain_agent_model_error_terminates_without_hanging() -> None:
    # A hard model failure in a plain entry agent must not hang the run: it
    # terminates (RUN_STARTED..RUN_FINISHED). Note the current behaviour is a
    # graceful, empty completion rather than a RUN_ERROR — this test guards
    # against a regression to a silent hang.
    r = await run_pipeline(str(CONFIGS / "error_plain.yaml"), "go")
    assert r.types()[0] == "RUN_STARTED"
    assert r.types()[-1] in {"RUN_FINISHED", "RUN_ERROR"}
    assert r.text() == ""


async def test_swarm_node_error_is_isolated_and_run_terminates() -> None:
    # A mid-swarm failure must terminate the run (never stall) and be isolated
    # to the failing node: the earlier node stays completed and the failing
    # node's card is marked "error".
    r = await run_pipeline(str(CONFIGS / "error_swarm.yaml"), "go")
    assert r.finished() or r.errored()

    planner = next(g for g in r.groups.values() if g["agentName"] == "planner")
    assert planner["status"] == "completed"

    researcher = next(g for g in r.groups.values() if g["agentName"] == "researcher")
    assert researcher["status"] == "error"

    # The writer (after the failing researcher) never produces its text.
    assert "unreached" not in r.text()


# --- interrupt / resume ---------------------------------------------------


# Interrupts must be first-class for EVERY agent position — a plain entry
# agent, a delegate sub-agent, and a swarm/graph node — with no caveats.
# (config file, expected substring in the resumed chat reply)
INTERRUPT_CASES = {
    "plain_entry": ("interrupt_plain", "Proceeding after approval."),
    "delegate_subagent": ("interrupt", "wrapped up after approval"),
    "swarm_node": ("interrupt_swarm", "Final swarm report after approval."),
    "graph_node": ("interrupt_graph", "Final graph report after approval."),
}


@pytest.mark.parametrize("case", list(INTERRUPT_CASES))
async def test_ask_user_interrupt_then_resume(case: str) -> None:
    config, expected = INTERRUPT_CASES[case]
    pipe = Pipeline(str(CONFIGS / f"{config}.yaml"))

    # Turn 1: an agent calls ask_user, pausing the whole run.
    r1 = await pipe.turn("start the task", thread_id="c1", run_id="r1")
    outcome = r1.run_outcome()
    assert outcome is not None, f"{case}: no interrupt outcome"
    assert getattr(outcome, "type", None) == "interrupt"
    interrupts = getattr(outcome, "interrupts", []) or []
    assert interrupts, f"{case}: no interrupts in outcome"
    interrupt_id = getattr(interrupts[0], "id", None)
    assert interrupt_id
    # Nothing final streamed yet — the run is paused awaiting the user.
    assert r1.text() == ""

    # Turn 2: resume with the user's answer; the run finishes cleanly.
    r2 = await pipe.turn(
        "(resume)",
        thread_id="c1",
        run_id="r2",
        resume=[{"interruptId": interrupt_id, "status": "resolved", "payload": {"answer": "yes"}}],
    )
    assert not r2.errored(), f"{case}: resumed run errored"
    assert r2.finished()
    assert expected in r2.text(), f"{case}: expected {expected!r} in {r2.text()!r}"


# --- multi-turn history round-trip ----------------------------------------


async def test_subagent_history_round_trips_and_accumulates() -> None:
    pipe = Pipeline(str(CONFIGS / "history.yaml"))

    r1 = await pipe.turn("first note", thread_id="h1", run_id="r1")
    assert not r1.errored()
    assert r1.exchange.outbound, "sub-agent produced no captured history"
    key = next(iter(r1.exchange.outbound))
    len1 = len(r1.exchange.outbound[key])
    assert len1 > 0

    # Feed turn 1's captured history back in, exactly as the client would.
    state = {"kaboo_history": r1.exchange.outbound}
    r2 = await pipe.turn("second note", thread_id="h1", run_id="r2", state=state)
    assert not r2.errored()
    assert key in r2.exchange.outbound
    # Seeded from prior transcript, then appended this turn's exchange.
    assert len(r2.exchange.outbound[key]) > len1


async def test_history_disabled_sub_agent_does_not_persist() -> None:
    # The delegate composition (no history:) must not accumulate transcripts,
    # confirming statelessness is the default.
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go")
    assert r.exchange.outbound == {}
