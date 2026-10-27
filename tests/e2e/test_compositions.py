"""End-to-end happy-path coverage for every supported orchestration composition.

Each case drives a real resolved pipeline through the production AG-UI adapter
with a deterministic scripted model and asserts:

- the AG-UI event stream is well-formed (starts RUN_STARTED, ends RUN_FINISHED,
  never RUN_ERROR) and the chat reply is the expected text, and
- the expected agents ran: as sub-agents (``SUBAGENT_*`` events, folded into
  groups) when a delegate reaches them, or as the steps ag-ui-strands reports
  for the nodes of a Swarm or Graph entry, which its own bridge drives. That
  bridge reports a Graph's top-level nodes only: agents inside a nested Swarm
  or Graph node, or behind a delegate node, are not on the wire.

Structural invariants (below) are checked for *all* shapes, so a regression in
sub-agent nesting for any mode fails loudly regardless of the specific fixture.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kaboo_workflows.testing import TurnResult, run_pipeline

CONFIGS = Path(__file__).parent / "configs"


@dataclass
class Case:
    config: str
    text: str  # expected chat-reply text (substring)
    agents: set[str]  # agents expected to run, as sub-agents or as Swarm/Graph steps
    chat_reply: str | None  # unused since the Swarm/Graph bridge moved upstream
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
        {"strategist", "editor"},
        "editor",
        set(),
    ),
    "graph_graph": Case(
        "graph_graph",
        "Outer end final report.",
        {"outer_start", "outer_end"},
        "outer_end",
        set(),
    ),
    "kitchen_sink": Case(
        "kitchen_sink",
        "Edited final report.",
        {"strategist", "editor", "audit_team"},
        "editor",
        set(),
    ),
}


def _steps(r: TurnResult) -> set[str]:
    """The agents ag-ui-strands reported as steps of a Swarm or Graph entry."""
    return {
        str(e.step_name).removeprefix("agent:")
        for e in r.events
        if e.type.value == "STEP_STARTED" and str(e.step_name).startswith("agent:")
    }


def _assert_invariants(r: TurnResult) -> None:
    types = r.types()
    assert types, "no events produced"
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    assert not r.errored()

    for gid, g in r.groups.items():
        # Every sub-agent finished cleanly on a deterministic happy path.
        assert g["status"] == "completed", f"{gid} status={g['status']}"
        # The group id's last segment is always the agent's name.
        assert gid.rsplit(".", 1)[-1] == g["agentName"], f"{gid} != {g['agentName']}"
        # A parent is another sub-agent of the run, never an invention.
        parent = g.get("parentGroup")
        assert parent is None or parent in r.groups, f"{gid} has an unknown parent {parent}"
        # Every sub-agent was started by a delegate call.
        assert g["toolCallId"], f"{gid} has no delegating call"


@pytest.mark.parametrize("name", list(CASES))
async def test_composition(name: str) -> None:
    case = CASES[name]
    r = await run_pipeline(str(CONFIGS / f"{case.config}.yaml"), "do the work please")

    _assert_invariants(r)
    assert case.text in r.text(), f"expected {case.text!r} in {r.text()!r}"

    got_agents = r.agent_names() | _steps(r)
    assert case.agents <= got_agents, f"missing {case.agents - got_agents}"

    for agent in case.delegated:
        assert any(g["agentName"] == agent for g in r.groups.values()), f"no sub-agent {agent}"


async def test_a_plain_entry_reports_no_sub_agents() -> None:
    # A plain entry agent is the chat itself, streamed by ag-ui-strands.
    r = await run_pipeline(str(CONFIGS / "plain.yaml"), "hi")
    assert r.groups == {}
    assert "SUBAGENT_STARTED" not in r.types()


async def test_a_delegate_entrys_calls_start_its_sub_agents() -> None:
    # The coordinator's delegations are its own tool calls in the chat; each
    # worker is a sub-agent started by one of them.
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go")
    calls = {
        e.tool_call_id: e.tool_call_name
        for e in r.events
        if e.type.value == "TOOL_CALL_START" and not e.subagent_run_id
    }
    assert sorted(calls.values()) == ["worker_a", "worker_b"]
    workers = {g["agentName"]: g["toolCallId"] for g in r.groups.values()}
    assert workers == {name: call for call, name in calls.items()}


async def test_delegate_two_levels_of_nesting() -> None:
    # The depth-2 delegate shows fact_checker nested under the team that
    # delegated to it: its parent is the sub-agent that made the call.
    r = await run_pipeline(str(CONFIGS / "delegate_2level.yaml"), "go")
    fact = next(g for g in r.groups.values() if g["agentName"] == "fact_checker")
    parent = fact["parentGroup"]
    assert parent in r.groups, f"fact_checker orphaned: parent {parent!r} not emitted"
    assert r.groups[parent]["agentName"] == "research_team"


async def test_graph_parallel_batch_all_run() -> None:
    # Both parallel nodes (writer, researcher) and the merge node (editor) run.
    r = await run_pipeline(str(CONFIGS / "graph.yaml"), "go")
    assert {"strategist", "writer", "researcher", "editor"} <= _steps(r)


async def test_kitchen_sink_mixes_swarm_and_delegate_in_one_graph() -> None:
    r = await run_pipeline(str(CONFIGS / "kitchen_sink.yaml"), "go")
    assert not r.errored()
    assert "Edited final report." in r.text()
    assert {"strategist", "editor"} <= _steps(r)


async def test_a_sub_agents_tool_call_carries_the_label_the_workflow_gives_it() -> None:
    config = """
x-model: &m kaboo_workflows.testing:ScriptedModel
agents:
  coordinator:
    model: {provider: *m, model_id: scripted, params: {script: [{tool: filer, input: "file it"}], final_text: "Done."}}
  filer:
    tools: ["tests.fakes.gated_tools:submit_report"]
    tool_labels: {submit_report: "Filing the report"}
    model:
      provider: *m
      model_id: scripted
      params: {script: [{tool: submit_report, input: {title: "Q3"}}], final_text: "Filed."}
orchestrations:
  team: {mode: delegate, entry_name: coordinator, connections: [{agent: filer, description: "Files reports"}]}
entry: team
"""
    r = await run_pipeline(config, "go")
    assert not r.errored()
    start = next(e for e in r.events if getattr(e, "tool_call_name", None) == "submit_report")
    assert start.metadata == {"label": "Filing the report"}
    [group] = r.groups.values()
    assert group["tools"][0]["toolLabel"] == "Filing the report"


async def test_a_sub_agents_tool_results_say_whether_they_failed_and_why() -> None:
    config = """
x-model: &m kaboo_workflows.testing:ScriptedModel
agents:
  coordinator:
    model: {provider: *m, model_id: scripted, params: {script: [{tool: builder, input: "build it"}], final_text: "Done."}}
  builder:
    tools:
      - "tests.fakes.failing_tools:query_warehouse"
      - "tests.fakes.failing_tools:create_panel"
      - "tests.fakes.data_tools:run_query"
    tool_labels: {create_panel: "Creating a panel"}
    model:
      provider: *m
      model_id: scripted
      params:
        script:
          - {tool: query_warehouse, input: {sql: "select 1"}}
          - {tool: create_panel, input: {kind: "pie3d"}}
          - {tool: run_query, input: {sql: "select region"}}
        final_text: "Built."
orchestrations:
  team: {mode: delegate, entry_name: coordinator, connections: [{agent: builder, description: "Builds dashboards"}]}
entry: team
"""
    r = await run_pipeline(config, "go")

    assert not r.errored()
    names = {
        e.tool_call_id: e.tool_call_name
        for e in r.events
        if e.type.value == "TOOL_CALL_START" and e.subagent_run_id
    }
    results = {
        names[e.tool_call_id]: e.metadata
        for e in r.events
        if e.type.value == "TOOL_CALL_RESULT" and e.subagent_run_id
    }
    assert results["query_warehouse"] == {"status": "error", "error": "The warehouse is offline."}
    assert results["create_panel"] == {
        "label": "Creating a panel",
        "status": "error",
        "error": "Unknown panel type: pie3d",
        "fix": "Use one of: bar, line, table",
    }
    assert results["run_query"] == {"status": "success"}


async def test_the_entry_agents_tool_results_say_whether_they_failed_and_why() -> None:
    config = """
agents:
  builder:
    tools:
      - "tests.fakes.failing_tools:create_panel"
      - "tests.fakes.data_tools:run_query"
    model:
      provider: kaboo_workflows.testing:ScriptedModel
      model_id: scripted
      params:
        script:
          - {tool: create_panel, input: {kind: "pie3d"}}
          - {tool: run_query, input: {sql: "select region"}}
        final_text: "Built."
entry: builder
"""
    r = await run_pipeline(config, "go")

    assert not r.errored()
    names = {
        e.tool_call_id: e.tool_call_name for e in r.events if e.type.value == "TOOL_CALL_START"
    }
    results = {
        names[e.tool_call_id]: e.metadata for e in r.events if e.type.value == "TOOL_CALL_RESULT"
    }
    assert results["create_panel"] == {
        "status": "error",
        "error": "Unknown panel type: pie3d",
        "fix": "Use one of: bar, line, table",
    }
    assert results["run_query"] == {"status": "success"}
