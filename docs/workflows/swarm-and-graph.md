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

## What you'll observe

- Swarm members (`researcher_a`, `researcher_b`) nest under the swarm node.
- The delegate's `checker` nests under the audit sub-tree via its tool call.
- Only the `editor` (the `chat_output`) carries the chat reply.

## Proven by

`tests/e2e/test_compositions.py::test_kitchen_sink_mixes_swarm_and_delegate_in_one_graph`.
