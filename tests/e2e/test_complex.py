"""End-to-end proofs for the complex, multi-depth workflow guides.

Each test here is the executable proof behind a ``docs/workflows/*.md`` guide and
an ``examples/`` project: it drives a deterministic scripted pipeline through the
production AG-UI path and asserts the exact multi-agent behaviour the guide
claims. Reuses the shared harness and, where a shape already exists, the shipped
``tests/e2e/configs`` fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .harness import Pipeline, run_pipeline

CONFIGS = Path(__file__).parent / "configs"


def _ancestor_ids(groups: dict, group_id: str) -> list[str]:
    """Walk parentGroup links upward, returning every ancestor group id."""
    chain: list[str] = []
    current = groups.get(group_id, {}).get("parentGroup")
    while current is not None:
        chain.append(current)
        current = groups.get(current, {}).get("parentGroup")
    return chain


# --- 1. Deep nested delegation (3 levels) ---------------------------------


async def test_delegate_three_levels_of_nesting() -> None:
    # top(coordinator) -> research_team(lead) -> field_team(field_researcher -> verifier).
    # The deepest leaf (verifier) must nest transitively under research_team, and
    # the coordinator's reply wraps the whole chain.
    r = await run_pipeline(str(CONFIGS / "delegate_3level.yaml"), "go")
    assert r.finished() and not r.errored()
    assert "Coordinator wrapped the chain." in r.text()

    verifier = next(gid for gid, g in r.groups.items() if g["agentName"] == "verifier")
    ancestors = _ancestor_ids(r.groups, verifier)
    assert ancestors, "verifier has no parent chain"
    assert any("research_team" in a for a in ancestors), (
        f"verifier not nested under research_team; ancestors={ancestors}"
    )
    # The immediate parent is a real emitted group (the leaf is not orphaned). The
    # top entry (coordinator) runs AS the orchestration and collapses, so it is a
    # path prefix rather than its own card — mirroring the 2-level nesting case.
    assert r.groups[verifier]["parentGroup"] in r.groups, "verifier orphaned"


# --- 3. Parallel top-level + nested parallelism ---------------------------


async def test_parallel_top_and_nested_batches_all_run() -> None:
    # Top graph runs writer + subteam in parallel; the subteam graph runs sa + sb
    # in parallel; the editor merge sees every branch.
    r = await run_pipeline(str(CONFIGS / "parallel_nested.yaml"), "go")
    assert r.finished() and not r.errored()

    names = r.agent_names()
    assert {"strategist", "writer", "sa", "sb", "sub_end", "editor"} <= names, (
        f"missing branches: {names}"
    )
    # The merge node's text is the single chat reply.
    reply_agents = {g["agentName"] for g in r.groups.values() if g.get("isChatReply")}
    assert reply_agents == {"editor"}, f"chat-reply {reply_agents}"
    assert "Edited final report." in r.text()


# --- 4. Multi-depth HITL incl. parallel tool calls ------------------------


async def test_parallel_interrupts_surface_together_and_resume() -> None:
    # A plain agent asks TWO questions in one step: both interrupts surface in the
    # same outcome with distinct ids, and resolving both finishes the run cleanly.
    pipe = Pipeline(str(CONFIGS / "interrupt_parallel.yaml"))

    r1 = await pipe.turn("start", thread_id="p1", run_id="r1")
    outcome = r1.run_outcome()
    assert outcome is not None and getattr(outcome, "type", None) == "interrupt"
    interrupts = list(getattr(outcome, "interrupts", []) or [])
    assert len(interrupts) == 2, f"expected 2 interrupts, got {len(interrupts)}"
    ids = [getattr(i, "id", None) for i in interrupts]
    assert all(ids) and len(set(ids)) == 2, f"interrupt ids not distinct: {ids}"
    assert r1.text() == ""

    resume = [{"interruptId": i, "status": "resolved", "payload": {"answer": "yes"}} for i in ids]
    r2 = await pipe.turn("(resume)", thread_id="p1", run_id="r2", resume=resume)
    assert not r2.errored() and r2.finished()
    assert "Both steps approved." in r2.text()


# --- 6. Rejection path ----------------------------------------------------


async def test_rejected_interrupt_finishes_cleanly() -> None:
    # Declining a gated tool (resume status != resolved) must resume to a clean
    # RUN_FINISHED — never hang or error. The tool receives the cancellation
    # sentinel and the agent proceeds without fabricating an approval.
    pipe = Pipeline(str(CONFIGS / "interrupt_plain.yaml"))

    r1 = await pipe.turn("start the task", thread_id="x1", run_id="r1")
    outcome = r1.run_outcome()
    assert outcome is not None and getattr(outcome, "type", None) == "interrupt"
    interrupt_id = getattr(list(outcome.interrupts)[0], "id", None)
    assert interrupt_id

    r2 = await pipe.turn(
        "(decline)",
        thread_id="x1",
        run_id="r2",
        resume=[{"interruptId": interrupt_id, "status": "cancelled"}],
    )
    assert not r2.errored() and r2.finished()
    assert "Proceeding after approval." in r2.text()


# --- 4b.3 every e2e config drives a well-formed stream --------------------


def _all_e2e_configs() -> list:
    return [pytest.param(str(p), id=p.stem) for p in sorted(CONFIGS.glob("*.yaml"))]


@pytest.mark.parametrize("config_path", _all_e2e_configs())
async def test_every_e2e_config_drives_a_wellformed_stream(config_path: str) -> None:
    # Smoke superset: no e2e config can be added without at least a well-formed
    # RUN_STARTED..RUN_FINISHED/RUN_ERROR stream on a single turn.
    r = await run_pipeline(config_path, "go")
    types = r.types()
    assert types, f"{config_path}: no events"
    assert types[0] == "RUN_STARTED", f"{config_path}: starts {types[0]}"
    assert types[-1] in {"RUN_FINISHED", "RUN_ERROR"}, f"{config_path}: ends {types[-1]}"
