# Swarm + graph combined

A graph's parallel batch can mix orchestration **kinds**. Here one branch is an
autonomous peer swarm and the other is a delegating coordinator — both run in
parallel, then a merge node combines them.

```
pipeline (graph):
  strategist ─▶ [ research_team (swarm) , audit_team (delegate) ] ─▶ editor
```

## Config

```yaml
orchestrations:
  research_team:
    mode: swarm
    agents: [researcher_a, researcher_b]
    entry_name: researcher_a
  audit_team:
    mode: delegate
    entry_name: auditor
    connections:
      - { agent: checker, description: "Focused compliance/accuracy check" }
  pipeline:
    mode: graph
    entry_name: strategist
    chat_output: editor
    edges:
      - { from: strategist, to: research_team }
      - { from: strategist, to: audit_team }
      - { from: research_team, to: editor }
      - { from: audit_team, to: editor }

entry: pipeline
```

Full runnable project: [`examples/16_swarm_in_graph`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/16_swarm_in_graph).

## Run

```bash
uv run python examples/16_swarm_in_graph/main.py
```

## What a Swarm or Graph entry supports

A Swarm or Graph *entry* runs on ag-ui-strands' own orchestrator bridge, not on
kaboo's sub-agent stream. Inside a delegate — a coordinator that calls a swarm
or a graph as a tool — they are ordinary sub-agents and get everything a
delegate does. As the entry, what they get differs:

| Capability | Swarm or Graph entry |
|---|---|
| Streaming | Each node is a step (`STEP_STARTED` / `STEP_FINISHED`, named by node) with its handoffs; its text streams in the main transcript. |
| Sub-agent cards | No: nodes are steps, not `SUBAGENT_*` events. Only a Graph's top-level nodes are on the wire; agents inside a nested node or behind a delegate node are not. |
| Approvals | Yes, in one process. A paused node does not survive a restart: strands does not yet persist state for orchestration node agents. |
| Browser tools, state effects, workspace views, generative UI | Not tested through the bridge; use a delegate entry for these. |

Kaboo's workflows all use a delegate entry, which is why these gaps are
documented rather than closed. Lab scenarios:
[swarm-dashboard](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/swarm-dashboard.yaml)
and
[graph-dashboard](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/graph-dashboard.yaml).

## Proven by

- `tests/e2e/test_compositions.py` — every composition streams a well-formed
  run, and the expected agents run as sub-agents or as steps.
- `tests/e2e/test_compositions.py::test_kitchen_sink_mixes_swarm_and_delegate_in_one_graph`.
- `tests/e2e/test_cross_cutting.py::test_an_interrupt_then_resume` (the swarm
  and graph positions).
