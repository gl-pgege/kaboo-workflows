"""End-to-end coverage for cross-cutting behaviours across a run's lifecycle:

- error propagation (a failing node surfaces RUN_ERROR, never a hang),
- HITL interrupt → resume (ask_user pauses the run, resume continues it), and
- multi-turn client-driven history (a sub-agent's transcript round-trips through
  ``state.kaboo_sessions.history`` and accumulates across turns).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ag_ui.core import EventType as AGUIEventType

from kaboo_workflows.testing import Pipeline, ScriptedModel, run_pipeline

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
    # A mid-swarm failure must terminate the run (never stall) after the earlier
    # node finished, and the node after the failing one never runs.
    r = await run_pipeline(str(CONFIGS / "error_swarm.yaml"), "go")
    assert r.finished() or r.errored()

    finished = [e.step_name for e in r.of_type(AGUIEventType.STEP_FINISHED)]
    assert "agent:planner" in finished
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
async def test_an_interrupt_then_resume(case: str) -> None:
    config, expected = INTERRUPT_CASES[case]
    pipe = Pipeline(str(CONFIGS / f"{config}.yaml"))

    # Turn 1: an agent's gated call pauses the whole run.
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

    # Turn 2: resume with the person's approval; the run finishes cleanly.
    r2 = await pipe.turn(
        "(resume)",
        thread_id="c1",
        run_id="r2",
        resume=[
            {"interruptId": interrupt_id, "status": "resolved", "payload": {"status": "approved"}}
        ],
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
    assert persisted["kaboo_sessions"]["paused"]["interrupt_state"]["activated"] is True
    paused_call_id = r1.of_type(AGUIEventType.TOOL_CALL_START)[0].tool_call_id

    restarted = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))
    r2 = await restarted.turn(
        thread_id="c1",
        run_id="r2",
        state=persisted,
        # The transcript the host replays, carrying the tool call that paused.
        messages=r1.messages(),
        resume=[
            {"interruptId": interrupt_id, "status": "resolved", "payload": {"status": "approved"}}
        ],
    )

    assert not r2.errored()
    assert r2.finished()

    # The proof of resumption: the approved call ran, on a process that never
    # saw the request raised.
    results = r2.of_type(AGUIEventType.TOOL_CALL_RESULT)
    assert [r.tool_call_id for r in results] == [paused_call_id]
    assert "submitted" in results[0].content

    # And the answered gate is spent, not replayed into the next turn.
    resumed_state = r2.state()["kaboo_sessions"]["paused"]["interrupt_state"]
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


async def test_a_new_message_on_a_restarted_service_does_not_replay_an_answered_gate() -> None:
    """A fresh run leaves behind a gate the replayed state still carries.

    A host that serves the next turn from a snapshot older than the resume's
    last one hands over the gate still activated, though the transcript shows
    it answered. Restoring it made the fresh run resume the answered call
    instead of reading the new message; on a workflow without that tool the
    result was "Unknown tool", and Bedrock refused every later turn of the
    thread.
    """
    from ag_ui.core import UserMessage

    first = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))
    r1 = await first.turn("start the task", thread_id="c5", run_id="r1")
    interrupt_id = getattr((getattr(r1.run_outcome(), "interrupts", []) or [])[0], "id", None)
    assert interrupt_id
    answered_call_id = r1.of_type(AGUIEventType.TOOL_CALL_START)[0].tool_call_id
    stale = r1.state()
    assert stale["kaboo_sessions"]["paused"]["interrupt_state"]["activated"] is True

    r2 = await first.turn(
        thread_id="c5",
        run_id="r2",
        state=stale,
        messages=r1.messages(),
        resume=[
            {"interruptId": interrupt_id, "status": "resolved", "payload": {"status": "approved"}}
        ],
    )
    assert not r2.errored() and r2.finished()

    restarted = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))
    r3 = await restarted.turn(
        thread_id="c5",
        run_id="r3",
        state=stale,
        messages=[*r2.messages(), UserMessage(id="next", role="user", content="what next?")],
    )

    assert not r3.errored()
    results = r3.of_type(AGUIEventType.TOOL_CALL_RESULT)
    assert answered_call_id not in [r.tool_call_id for r in results]
    carried = r3.state().get("kaboo_sessions", {}).get("paused", {}).get("interrupt_state", {})
    assert interrupt_id not in carried.get("interrupts", {})


async def test_swarm_tool_gate_pauses_then_executes_the_edited_call() -> None:
    # A gated PLATFORM-STYLE tool (interrupt.tools) inside a swarm node: the
    # gate pauses the run before the tool runs, the descriptor carries the card
    # fields (tool_name, tool_input), and an approve-with-edits resume executes
    # exactly the edited call. ag-ui-strands' Swarm bridge does not put the
    # call's id on the interrupt.
    from tests.fakes.gated_tools import CALLS

    CALLS.clear()
    pipe = Pipeline(str(CONFIGS / "interrupt_swarm_gate.yaml"))

    r1 = await pipe.turn("file the report", thread_id="g1", run_id="r1")
    outcome = r1.run_outcome()
    assert outcome is not None and getattr(outcome, "type", None) == "interrupt"
    gate = list(outcome.interrupts)[0]
    reason = gate.metadata or {}
    assert reason.get("tool_name") == "submit_report"
    assert reason.get("tool_input") == {"title": "Q3 draft"}
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


# --- response size --------------------------------------------------------


async def test_a_run_streams_only_what_its_turn_adds() -> None:
    """A response grows with its turn, never with the thread.

    No run re-sends the conversation as a ``MESSAGES_SNAPSHOT``. The client
    builds the transcript from the events, sub-agents' messages included, and
    sends it back with the next turn.
    """
    from ag_ui.core import UserMessage

    pipe = Pipeline(str(CONFIGS / "history.yaml"))
    r1 = await pipe.turn("first note", thread_id="s1", run_id="r1")
    r2 = await pipe.turn(
        thread_id="s1",
        run_id="r2",
        state=r1.state(),
        messages=[*r1.messages(), UserMessage(id="u2", role="user", content="second note")],
    )

    for r in (r1, r2):
        assert not r.errored() and r.finished()
        assert AGUIEventType.MESSAGES_SNAPSHOT not in {e.type for e in r.events}
    assert any(getattr(m, "subagent_run_id", None) for m in r1.messages())
    earlier = {m.id for m in r1.messages()}
    assert not earlier & {getattr(e, "message_id", None) for e in r2.events}
    assert "Coordinator done." in r2.text()


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
    state = {"kaboo_sessions": {"history": r1.exchange.outbound}}
    r2 = await pipe.turn("second note", thread_id="h1", run_id="r2", state=state)
    assert not r2.errored()
    assert key in r2.exchange.outbound
    # Seeded from prior transcript, then appended this turn's exchange.
    assert len(r2.exchange.outbound[key]) > len1


def _texts(messages: list[dict]) -> list[str]:
    return [block["text"] for m in messages for block in m["content"] if "text" in block]


async def test_a_forked_delegate_entry_remembers_through_its_entry_agents_group() -> None:
    # analyze_sql is a fork of sql_analyst (history: {group: sql}). Each turn
    # builds a fresh session, as a per-run config does, so the second turn can
    # only know the first through the state the host replays.
    pipe = Pipeline(
        str(CONFIGS / "history_delegate_entry.yaml"), session_config_key="workflow_config"
    )
    analyst = pipe.infra.models["analyst"]
    assert isinstance(analyst, ScriptedModel)

    r1 = await pipe.turn("first question", thread_id="d1", run_id="r1")
    assert not r1.errored()
    assert _texts(r1.state()["kaboo_sessions"]["history"]["sql"]) == [
        "how much revenue?",
        "Revenue is 42.",
    ]

    r2 = await pipe.turn("second question", thread_id="d1", run_id="r2", state=r1.state())

    assert not r2.errored()
    assert _texts(analyst.calls[-1]) == ["how much revenue?", "Revenue is 42.", "how much revenue?"]
    assert len(r2.state()["kaboo_sessions"]["history"]["sql"]) == 4


def _result_texts(messages: list[dict]) -> list[str]:
    return [
        block["toolResult"]["content"][0]["text"]
        for m in messages
        for block in m["content"]
        if "toolResult" in block
    ]


async def test_a_masked_sub_agent_writes_back_a_paused_call_whole_and_a_finished_one_masked():
    pipe = Pipeline(str(CONFIGS / "history_masked.yaml"))

    r1 = await pipe.turn("revenue by region", thread_id="m1", run_id="r1")

    outcome = r1.run_outcome()
    assert getattr(outcome, "type", None) == "interrupt"
    paused = r1.state()["kaboo_sessions"]["history"]["sql"]
    assert any("region-199" in text for text in _result_texts(paused))

    r2 = await pipe.turn(
        "(resume)",
        thread_id="m1",
        run_id="r2",
        resume=[{"interruptId": outcome.interrupts[0].id, "status": "resolved", "payload": "no"}],
    )

    assert r2.finished() and not r2.errored()
    finished = r2.state()["kaboo_sessions"]["history"]["sql"]
    assert (
        "Result cleared (run_query, queryExecutionId=q-123): call it again to see it."
        in _result_texts(finished)
    )
    assert _texts(finished)[0] == "revenue by region"
    assert _texts(finished)[-1] == "Revenue by region is ready."
    assert any("region-199" in text for text in _result_texts(r2.exchange.outbound["sql"]))


def _analyst_call(n: int, report: str) -> list[dict]:
    rows = json.dumps({"queryExecutionId": f"q-{n}", "padding": "x" * 2000})
    use = {"toolUseId": f"c{n}", "name": "athena___run_query", "input": {"n": n}}
    result = {"toolUseId": f"c{n}", "status": "success", "content": [{"text": rows}]}
    return [
        {"role": "user", "content": [{"text": f"question {n}"}]},
        {"role": "assistant", "content": [{"toolUse": use}]},
        {"role": "user", "content": [{"toolResult": result}]},
        {"role": "assistant", "content": [{"text": report}]},
    ]


async def test_a_memory_over_summarize_over_is_written_back_with_its_oldest_calls_summarised():
    remember = "## To remember\n- revenue is stored in cents"
    stored = [
        *_analyst_call(1, f"Revenue is $42M.\n\n{remember}"),
        *_analyst_call(2, "Region data is in sales_by_region."),
        *_analyst_call(3, "Returns are 3%."),
    ]
    pipe = Pipeline(str(CONFIGS / "history_summary.yaml"))

    r = await pipe.turn(
        "and by region?",
        thread_id="s1",
        run_id="r1",
        state={"kaboo_sessions": {"history": {"sql": stored}}},
    )

    assert not r.errored()
    memory = r.state()["kaboo_sessions"]["history"]["sql"]
    assert _texts(memory[:2]) == [
        "Summary of your earlier work in this conversation",
        "Earlier, queries q-1 and q-2 found $42M of revenue.",
        remember,
    ]
    assert _texts(memory)[-2:] == ["and by region?", "By region it is ready."]


async def test_history_disabled_sub_agent_does_not_persist() -> None:
    # The delegate composition (no history:) must not accumulate transcripts,
    # confirming statelessness is the default.
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go")
    assert r.exchange.outbound == {}
