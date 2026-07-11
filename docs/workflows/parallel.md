# Parallel (top-level + nested)

A graph fans out to parallel branches by giving one source node multiple outgoing
edges. Because a graph node can itself be an orchestration, you get nested
parallelism for free.

```
pipeline (graph):
  strategist ─▶ [ writer , subteam ] ─▶ editor
  subteam (graph): sub_lead ─▶ [ analyst_a , analyst_b ] ─▶ sub_end
```

## Config

```yaml
orchestrations:
  subteam:
    mode: graph
    entry_name: sub_lead
    chat_output: sub_end
    edges:
      - { from: sub_lead, to: analyst_a }
      - { from: sub_lead, to: analyst_b }
      - { from: analyst_a, to: sub_end }
      - { from: analyst_b, to: sub_end }
  pipeline:
    mode: graph
    entry_name: strategist
    chat_output: editor
    edges:
      - { from: strategist, to: writer }
      - { from: strategist, to: subteam }
      - { from: writer, to: editor }
      - { from: subteam, to: editor }

entry: pipeline
```

Full runnable project: [`examples/17_parallel`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/17_parallel).

## Run

```bash
uv run python examples/17_parallel/main.py
```

## What you'll observe

- `writer` and `subteam` run in parallel after `strategist`.
- Inside `subteam`, `analyst_a` and `analyst_b` run in parallel before `sub_end`.
- `editor` only runs once both top-level branches finish, and owns the reply.

## Proven by

- `tests/e2e/test_complex.py::test_parallel_top_and_nested_batches_all_run`
  (all leaves run; editor merges).
- `tests/e2e/test_compositions.py::test_graph_parallel_batch_all_run`
  (top-level parallel batch).
