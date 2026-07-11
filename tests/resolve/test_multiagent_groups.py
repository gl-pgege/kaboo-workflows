"""Hierarchical stream groups + chat-output resolution for swarm/graph entries.

Covers robustness case R1 (mixed nesting): the dot-path stream-group builder
must compose correct ``parent`` paths across ALL orchestration types and their
nesting — not just delegate — or a member card is orphaned. Also pins the
``chat_output`` schema validation and resolver behaviour, and asserts the
delegate grouping is unchanged (R5, backend side).
"""

from __future__ import annotations

import pytest

from kaboo_workflows.config.resolvers.config import (
    _auto_resolve_stream_groups,
    _resolve_chat_output,
)
from kaboo_workflows.config.schema import AppConfig
from tests.factories import (
    agent_def,
    delegate_orchestration,
    graph_orchestration,
    swarm_orchestration,
)


def _agents(*names: str) -> dict:
    return {name: agent_def() for name in names}


def test_swarm_entry_members_nest_under_the_orchestration():
    config = AppConfig(
        agents=_agents("planner", "researcher", "writer"),
        orchestrations={
            "team": swarm_orchestration("planner", ["planner", "researcher", "writer"])
        },
        entry="team",
    )
    groups = _auto_resolve_stream_groups(config)

    assert groups["planner"][0] == "team.planner"
    assert groups["researcher"][0] == "team.researcher"
    assert groups["writer"][0] == "team.writer"


def test_graph_entry_nodes_nest_under_the_orchestration():
    config = AppConfig(
        agents=_agents("a", "b"),
        orchestrations={"pipe": graph_orchestration("a", [("a", "b")])},
        entry="pipe",
    )
    groups = _auto_resolve_stream_groups(config)

    assert groups["a"][0] == "pipe.a"
    assert groups["b"][0] == "pipe.b"


def test_swarm_in_graph_composes_full_dot_path():
    # R1 mixed nesting: a graph whose node is itself a swarm. The swarm's
    # members must carry the full graph.swarm.member path so the UI nests them.
    config = AppConfig(
        agents=_agents("cs", "p", "w", "mr", "ce"),
        orchestrations={
            "sw": swarm_orchestration("p", ["p", "w"]),
            "pipe": graph_orchestration(
                "cs", [("cs", "sw"), ("cs", "mr"), ("sw", "ce"), ("mr", "ce")]
            ),
        },
        entry="pipe",
    )
    groups = _auto_resolve_stream_groups(config)

    assert groups["cs"][0] == "pipe.cs"
    assert groups["mr"][0] == "pipe.mr"
    assert groups["ce"][0] == "pipe.ce"
    assert groups["sw"][0] == "pipe.sw"
    assert groups["p"][0] == "pipe.sw.p"
    assert groups["w"][0] == "pipe.sw.w"


def test_delegate_grouping_is_unchanged():
    # R5 (backend): the delegate path must not regress. Entry keeps its own
    # name; connections nest one level under the entry's group.
    config = AppConfig(
        agents=_agents("writer", "researcher"),
        orchestrations={"coord": delegate_orchestration("writer", {"researcher": "research"})},
        entry="coord",
    )
    groups = _auto_resolve_stream_groups(config)

    assert groups["writer"][0] == "writer"
    assert groups["researcher"][0] == "writer.researcher"


def _orphans(config: AppConfig, groups: dict) -> list[str]:
    """Return group paths whose parent (dot-path minus last segment) is not an
    emitted group — i.e. cards that can never be surfaced by drilling into a
    parent.

    Emitted groups are every agent path plus every orchestration path (an
    orchestrator publishes its own group at runtime; nested ones live in the
    map, the top one defaults to its own name). A one-segment path parents to
    the chat root, which is legitimately absent.
    """
    emitted = {g for g, _ in groups.values()}
    for name in config.orchestrations:
        emitted.add(groups.get(name, (name, name))[0])
    orphans = []
    for path in emitted:
        if "." not in path:
            continue  # top-level base; parent is the chat root
        if path.rsplit(".", 1)[0] not in emitted:
            orphans.append(path)
    return orphans


def test_nested_delegate_entry_collapses_children_not_orphaned():
    # delegate -> delegate -> plain. The inner delegate's entry (`lead`) runs AS
    # the orchestration, so it must collapse into the orchestration group and its
    # child must parent to that emitted group — not a phantom `...lead` group.
    config = AppConfig(
        agents=_agents("coordinator", "lead", "field_researcher"),
        orchestrations={
            "research_team": delegate_orchestration("lead", {"field_researcher": "research"}),
            "research_pipeline": delegate_orchestration(
                "coordinator", {"research_team": "the team"}
            ),
        },
        entry="research_pipeline",
    )
    groups = _auto_resolve_stream_groups(config)

    assert groups["coordinator"][0] == "coordinator"
    assert groups["research_team"][0] == "coordinator.research_team"
    # entry collapses into the orchestration group (same running fork)
    assert groups["lead"][0] == "coordinator.research_team"
    # child parents to the EMITTED orchestration group, not `...research_team.lead`
    assert groups["field_researcher"][0] == "coordinator.research_team.field_researcher"
    assert _orphans(config, groups) == []


def test_delegate_targeting_swarm_no_double_prefix():
    # delegate -> swarm. Swarm members must nest under `mgr.sw.<member>`, not
    # `mgr.sw.sw.<member>` (double) nor orphan.
    config = AppConfig(
        agents=_agents("mgr", "p", "w"),
        orchestrations={
            "sw": swarm_orchestration("p", ["p", "w"]),
            "coord": delegate_orchestration("mgr", {"sw": "a swarm"}),
        },
        entry="coord",
    )
    groups = _auto_resolve_stream_groups(config)

    assert groups["mgr"][0] == "mgr"
    assert groups["sw"][0] == "mgr.sw"
    assert groups["p"][0] == "mgr.sw.p"
    assert groups["w"][0] == "mgr.sw.w"
    assert _orphans(config, groups) == []


def test_delegate_nested_in_graph_collapses_entry():
    # graph -> delegate node. The delegate's entry collapses into the node group.
    config = AppConfig(
        agents=_agents("cs", "lead", "helper", "ce"),
        orchestrations={
            "team": delegate_orchestration("lead", {"helper": "helps"}),
            "pipe": graph_orchestration("cs", [("cs", "team"), ("team", "ce")]),
        },
        entry="pipe",
    )
    groups = _auto_resolve_stream_groups(config)

    assert groups["cs"][0] == "pipe.cs"
    assert groups["team"][0] == "pipe.team"
    assert groups["lead"][0] == "pipe.team"
    assert groups["helper"][0] == "pipe.team.helper"
    assert groups["ce"][0] == "pipe.ce"
    assert _orphans(config, groups) == []


def test_chat_output_resolves_explicit_swarm_node():
    config = AppConfig(
        agents=_agents("planner", "writer"),
        orchestrations={
            "team": swarm_orchestration("planner", ["planner", "writer"], chat_output="writer")
        },
        entry="team",
    )
    assert _resolve_chat_output(config) == "writer"


def test_chat_output_defaults_to_sole_graph_terminal():
    config = AppConfig(
        agents=_agents("a", "b"),
        orchestrations={"pipe": graph_orchestration("a", [("a", "b")])},
        entry="pipe",
    )
    # b is the only node with no outgoing edge → the default chat voice.
    assert _resolve_chat_output(config) == "b"


def test_chat_output_none_when_graph_terminal_is_ambiguous():
    # Two terminal nodes (b, c) → cannot pick statically; adapter emits the
    # final node's text at completion instead of streaming live.
    config = AppConfig(
        agents=_agents("a", "b", "c"),
        orchestrations={"pipe": graph_orchestration("a", [("a", "b"), ("a", "c")])},
        entry="pipe",
    )
    assert _resolve_chat_output(config) is None


def test_swarm_chat_output_must_name_a_member():
    with pytest.raises(ValueError, match="chat_output"):
        swarm_orchestration("planner", ["planner", "writer"], chat_output="ghost")


def test_graph_chat_output_must_name_a_node():
    with pytest.raises(ValueError, match="chat_output"):
        graph_orchestration("a", [("a", "b")], chat_output="ghost")
