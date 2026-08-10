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


async def test_approval_survives_a_restart_of_the_service() -> None:
    """A gate paused in one process is resumable in the next.

    The interrupt lives in the per-thread agent's memory, so without state on the
    wire a restart between the question and the answer stranded the approval: the
    analyst clicked approve and got "No agent session found for resume". Here the
    second turn runs on a *fresh* Pipeline — a new process, no shared agents — and
    carries nothing but the state snapshot the host persisted, as kaboo-runtime
    replays it.
    """
    first = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))
    r1 = await first.turn("start the task", thread_id="c1", run_id="r1")

    outcome = r1.run_outcome()
    assert getattr(outcome, "type", None) == "interrupt"
    interrupt_id = getattr((getattr(outcome, "interrupts", []) or [])[0], "id", None)
    assert interrupt_id

    persisted = r1.state()
    assert persisted["kaboo_session"]["interrupt_state"]["activated"] is True
    paused_call_id = r1.of_type(AGUIEventType.TOOL_CALL_START)[0].tool_call_id

    restarted = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))
    r2 = await restarted.turn(
        thread_id="c1",
        run_id="r2",
        state=persisted,
        # The transcript the host replays, carrying the tool call that paused.
        messages=r1.messages(),
        resume=[{"interruptId": interrupt_id, "status": "resolved", "payload": {"answer": "yes"}}],
    )

    assert not r2.errored()
    assert r2.finished()

    # The proof of resumption: the paused tool produced its result from the
    # user's answer, on a process that never saw the question asked.
    results = r2.of_type(AGUIEventType.TOOL_CALL_RESULT)
    assert [r.tool_call_id for r in results] == [paused_call_id]
    assert "yes" in results[0].content

    # And the answered gate is spent, not replayed into the next turn.
    resumed_state = r2.state()["kaboo_session"]["interrupt_state"]
    assert interrupt_id not in resumed_state["interrupts"]

    # Note: the final assistant text is not asserted here. Each Pipeline builds
    # its own ScriptedModel, and the fake picks its action by call index, so the
    # "restarted" one replays its first scripted step rather than answering. That
    # is the fake restarting with the process, not the resume failing.


async def test_resume_without_carried_state_still_reports_a_lost_session() -> None:
    """A cold resume with nothing to restore must fail loudly, not silently pass.

    Guards the seeding path from turning a genuinely lost session into a run that
    quietly approves nothing.
    """
    first = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))
    r1 = await first.turn("start the task", thread_id="c2", run_id="r1")
    interrupt_id = getattr((getattr(r1.run_outcome(), "interrupts", []) or [])[0], "id", None)

    restarted = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))
    with pytest.raises(LookupError):
        await restarted.turn(
            "(resume)",
            thread_id="c2",
            run_id="r2",
            resume=[{"interruptId": interrupt_id, "status": "resolved", "payload": {}}],
        )


async def test_swarm_tool_gate_pauses_then_executes_the_edited_call() -> None:
    # A gated PLATFORM-STYLE tool (interrupt.tools) inside a swarm node: the
    # gate pauses the run before the tool runs, the descriptor carries the
    # correlation/card fields (toolCallId, tool_name, tool_input), and an
    # approve-with-edits resume executes exactly the edited call.
    from tests.fakes.gated_tools import CALLS

    CALLS.clear()
    pipe = Pipeline(str(CONFIGS / "interrupt_swarm_gate.yaml"))

    r1 = await pipe.turn("file the report", thread_id="g1", run_id="r1")
    outcome = r1.run_outcome()
    assert outcome is not None and getattr(outcome, "type", None) == "interrupt"
    gate = list(outcome.interrupts)[0]
    assert gate.tool_call_id, "gate descriptor must carry the originating toolCallId"
    metadata = gate.metadata or {}
    assert metadata.get("tool_name") == "submit_report"
    assert metadata.get("tool_input") == {"title": "Q3 draft"}
    assert CALLS == [], "gated tool must not execute before approval"

    r2 = await pipe.turn(
        "(approve)",
        thread_id="g1",
        run_id="r2",
        resume=[
            {
                "interruptId": gate.id,
                "status": "resolved",
                "payload": {"status": "approved", "tool_input": {"title": "Q3 final"}},
            }
        ],
    )
    assert not r2.errored() and r2.finished()
    assert CALLS == [{"title": "Q3 final"}]
    assert "Final swarm report after the gated write." in r2.text()


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
