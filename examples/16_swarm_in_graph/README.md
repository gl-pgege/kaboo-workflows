# 16 — Swarm + graph combined

> A graph's parallel batch can mix orchestration *kinds*: an autonomous peer
> swarm and a delegating coordinator run side by side, then merge.

## What this shows

- **Heterogeneous parallel batch** — `research_team` (swarm) and `audit_team`
  (delegate) are two branches of the same graph, executed in parallel.
- **Nested activity grouping** — swarm members nest under the swarm node; the
  delegate's `checker` nests under the audit sub-tree via its tool call.
- **Single merge/chat reply** — only the `editor` node's text is the chat bubble.

## Shape

```
pipeline (graph):
  strategist ─▶ [ research_team (swarm) , audit_team (delegate) ] ─▶ editor
```

## Run

```bash
uv run python examples/16_swarm_in_graph/main.py
```

## Proven by

`tests/e2e/test_compositions.py::test_kitchen_sink_mixes_swarm_and_delegate_in_one_graph`
asserts swarm members nest under the swarm node, the delegate's checker nests via
`toolCallId`, and only the editor carries the chat reply. See the
[Swarm + graph guide](../../docs/workflows/swarm-and-graph.md).
