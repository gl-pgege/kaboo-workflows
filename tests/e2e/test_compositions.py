"""End-to-end happy-path coverage for every supported orchestration composition.

Each case drives a real resolved pipeline through the production AG-UI adapter
with a deterministic scripted model and asserts:

- the AG-UI event stream is well-formed (starts RUN_STARTED, ends RUN_FINISHED,
  never RUN_ERROR) and the chat reply is the expected text, and
- the folded activity-group tree matches the expected agents, parent nesting,
  and chat-reply flag — i.e. the UI progress model is correct for that shape.

Structural invariants (below) are checked for *all* shapes, so a regression in
grouping/nesting for any mode fails loudly regardless of the specific fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from .harness import TurnResult, run_pipeline

CONFIGS = Path(__file__).parent / "configs"


@dataclass
class Case:
    config: str
    text: str  # expected chat-reply text (substring)
    agents: set[str]  # agentNames expected among activity groups
    chat_reply: str | None  # the group whose text IS the chat bubble, if any
    delegated: set[str]  # agents reached via a delegate tool (toolCallId set)


CASES = {
    "plain": Case("plain", "Hello from the plain agent.", set(), None, set()),
    "delegate": Case(
        "delegate",
        "Coordinator combined the results.",
        {"worker_a", "worker_b"},
        None,
        {"worker_a", "worker_b"},
    ),
    "delegate_2level": Case(
        "delegate_2level",
        "Coordinator wrapped up.",
        {"research_team", "fact_checker"},
        None,
        {"research_team", "fact_checker"},
    ),
    "delegate_swarm": Case(
        "delegate_swarm",
        "Coordinator returned the swarm's report.",
        {"planner", "researcher", "writer"},
        None,
        set(),
    ),
    "delegate_graph": Case(
        "delegate_graph",
        "Coordinator returned the graph's report.",
        {"strategist", "writer", "researcher", "editor"},
        None,
        set(),
    ),
    "swarm": Case(
        "swarm",
        "Final report from the swarm.",
        {"planner", "researcher", "writer"},
        "writer",
        set(),
    ),
    "graph": Case(
        "graph",
        "Edited final report.",
        {"strategist", "writer", "researcher", "editor"},
        "editor",
        set(),
    ),
    "graph_swarm": Case(
        "graph_swarm",
        "Edited final report.",
        {"strategist", "a", "b", "editor"},
        "editor",
        set(),
    ),
    "graph_graph": Case(
        "graph_graph",
        "Outer end final report.",
        {"outer_start", "g1", "g2", "outer_end"},
        "outer_end",
        set(),
    ),
    "kitchen_sink": Case(
        "kitchen_sink",
        "Edited final report.",
        {"strategist", "a", "b", "checker", "editor", "audit_team"},
        "editor",
        set(),
    ),
}


def _assert_invariants(r: TurnResult) -> None:
    types = r.types()
    assert types, "no events produced"
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert not r.errored()

    chat_replies = 0
    for gid, g in r.groups.items():
        # Every group finished cleanly on a deterministic happy path.
        assert g["status"] == "completed", f"{gid} status={g['status']}"

        # The group id's last segment is always the agent's name.
        assert gid.rsplit(".", 1)[-1] == g["agentName"], f"{gid} != {g['agentName']}"

        # parentGroup, when set, is a strict path-prefix of the group id.
        parent = g.get("parentGroup")
        if parent is not None:
            assert gid.startswith(parent + "."), f"{gid} not under {parent}"

        if g.get("isChatReply"):
            chat_replies += 1

    # At most one card carries the chat reply (avoids duplicating the bubble).
    assert chat_replies <= 1, f"{chat_replies} chat-reply groups"


@pytest.mark.parametrize("name", list(CASES))
async def test_composition(name: str) -> None:
    case = CASES[name]
    r = await run_pipeline(str(CONFIGS / f"{case.config}.yaml"), "do the work please")

    _assert_invariants(r)
    assert case.text in r.text(), f"expected {case.text!r} in {r.text()!r}"

    got_agents = r.agent_names()
    assert case.agents <= got_agents, f"missing {case.agents - got_agents}"

    # chat-reply flag lands on exactly the configured chat_output agent.
    reply_agents = {g["agentName"] for g in r.groups.values() if g.get("isChatReply")}
    if case.chat_reply is None:
        assert reply_agents == set(), f"unexpected chat-reply cards {reply_agents}"
    else:
        assert reply_agents == {case.chat_reply}, f"chat-reply {reply_agents}"

    # Delegated agents carry a toolCallId; peers/graph nodes do not.
    for agent in case.delegated:
        gids = [gid for gid, g in r.groups.items() if g["agentName"] == agent]
        assert gids, f"no group for delegated agent {agent}"
        assert all(r.groups[g].get("toolCallId") for g in gids), f"{agent} missing toolCallId"


async def test_plain_has_no_activity_groups() -> None:
    # A plain entry agent owns the chat bubble directly; it is never a card.
    r = await run_pipeline(str(CONFIGS / "plain.yaml"), "hi")
    assert r.groups == {}


async def test_delegate_two_levels_of_nesting() -> None:
    # The depth-2 delegate shows fact_checker nested under the team's group. The
    # inner delegate's entry (`lead`) runs AS the orchestration and collapses into
    # its group, so fact_checker parents to that EMITTED group — never a phantom
    # `...lead` group. Its parent MUST exist, else the card is orphaned and
    # invisible when drilling into the team.
    r = await run_pipeline(str(CONFIGS / "delegate_2level.yaml"), "go")
    fact = next(g for g in r.groups.values() if g["agentName"] == "fact_checker")
    parent = fact["parentGroup"]
    assert parent is not None
    assert "research_team" in parent
    assert "lead" not in parent
    assert parent in r.groups, f"fact_checker orphaned: parent {parent!r} not emitted"


async def test_graph_parallel_batch_all_run() -> None:
    # Both parallel nodes (writer, researcher) and the merge node (editor) run.
    r = await run_pipeline(str(CONFIGS / "graph.yaml"), "go")
    names = r.agent_names()
    assert {"strategist", "writer", "researcher", "editor"} <= names


async def test_kitchen_sink_mixes_swarm_and_delegate_in_one_graph() -> None:
    r = await run_pipeline(str(CONFIGS / "kitchen_sink.yaml"), "go")

    # Swarm members nest under the swarm node.
    for peer in ("a", "b"):
        g = next(gg for gg in r.groups.values() if gg["agentName"] == peer)
        assert g["parentGroup"] and g["parentGroup"].endswith("research_team")

    # The delegate's checker nests under the audit sub-tree via a tool call.
    checker = next(g for g in r.groups.values() if g["agentName"] == "checker")
    assert checker.get("toolCallId")
    assert checker["parentGroup"] and "audit_team" in checker["parentGroup"]

    # Only the editor's text is the chat reply.
    assert {g["agentName"] for g in r.groups.values() if g.get("isChatReply")} == {"editor"}
