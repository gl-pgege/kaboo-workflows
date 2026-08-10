"""End-to-end coverage for runs that submit their own workflow config.

A service in this mode behaves like a function: every turn brings the config it
wants, gets its own agents and entry, and leaves nothing behind. These tests hold
the two properties that makes safe — a later turn can be a different workflow,
and a conversation paused on an approval still resumes even though the agent that
paused it is gone.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from ag_ui.core import EventType as AGUIEventType

from .harness import Pipeline

CONFIGS = Path(__file__).parent / "configs"

KEY = "workflow_config"


def _speaks(text: str) -> str:
    """A one-agent workflow whose whole behaviour is the text it replies with."""
    return textwrap.dedent(f"""
        agents:
          assistant:
            stream: {{title: "Assistant"}}
            model:
              provider: tests.fakes.scripted_model:ScriptedModel
              model_id: scripted
              params:
                script: []
                final_text: "{text}"

        entry: assistant
    """)


async def test_consecutive_turns_can_be_different_workflows() -> None:
    # The point of the whole exercise: editing an agent type takes effect on the
    # next message, with no restart and no file written anywhere.
    pipe = Pipeline(str(CONFIGS / "plain.yaml"), session_config_key=KEY)

    r1 = await pipe.turn("go", thread_id="s1", run_id="r1", forwarded_props={KEY: _speaks("first")})
    r2 = await pipe.turn(
        "go", thread_id="s1", run_id="r2", forwarded_props={KEY: _speaks("second")}
    )

    assert "first" in r1.text()
    assert "second" in r2.text()


async def test_a_turn_without_a_submitted_config_runs_the_base() -> None:
    # Hosts roll this out gradually, so a run that sends no config must still work.
    pipe = Pipeline(str(CONFIGS / "plain.yaml"), session_config_key=KEY)

    r = await pipe.turn("go", thread_id="s2", run_id="r1")

    assert not r.errored()
    assert r.finished()


async def test_two_threads_running_different_workflows_do_not_bleed() -> None:
    # Isolation is by construction — each run resolves its own agents — but this
    # is the property a shared-agent implementation would quietly break.
    pipe = Pipeline(str(CONFIGS / "plain.yaml"), session_config_key=KEY)

    a = await pipe.turn("go", thread_id="a", run_id="r1", forwarded_props={KEY: _speaks("alpha")})
    b = await pipe.turn("go", thread_id="b", run_id="r1", forwarded_props={KEY: _speaks("beta")})

    assert "alpha" in a.text() and "beta" not in a.text()
    assert "beta" in b.text() and "alpha" not in b.text()


async def test_approval_survives_the_session_being_rebuilt() -> None:
    """The gate outlives the agent that opened it, within one live process.

    Per-run sessions mean the resuming turn never sees the clone that paused —
    the same situation as a restart, minus the restart. It resumes because the
    pending interrupt travels on the state channel with the turn.
    """
    gated = (CONFIGS / "interrupt_plain.yaml").read_text()
    pipe = Pipeline(str(CONFIGS / "plain.yaml"), session_config_key=KEY)

    r1 = await pipe.turn(
        "start the task", thread_id="s3", run_id="r1", forwarded_props={KEY: gated}
    )
    outcome = r1.run_outcome()
    assert getattr(outcome, "type", None) == "interrupt"
    interrupt_id = getattr((getattr(outcome, "interrupts", []) or [])[0], "id", None)
    assert interrupt_id
    paused_call_id = r1.of_type(AGUIEventType.TOOL_CALL_START)[0].tool_call_id

    r2 = await pipe.turn(
        thread_id="s3",
        run_id="r2",
        state=r1.state(),
        messages=r1.messages(),
        forwarded_props={KEY: gated},
        resume=[{"interruptId": interrupt_id, "status": "resolved", "payload": {"answer": "yes"}}],
    )

    assert not r2.errored()
    results = r2.of_type(AGUIEventType.TOOL_CALL_RESULT)
    assert [r.tool_call_id for r in results] == [paused_call_id]
    assert "yes" in results[0].content
