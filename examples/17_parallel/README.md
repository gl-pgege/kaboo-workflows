# 17 — Parallel (top-level + nested)

> A graph fans out to parallel branches, and one of those branches is itself a
> graph with its own parallel fan-out.

## What this shows

- **Top-level parallelism** — `writer` and `subteam` both run after `strategist`.
- **Nested parallelism** — inside `subteam`, `analyst_a` and `analyst_b` run in
  parallel before `sub_end` merges them.
- **Merge sees every branch** — `editor` only runs once both top-level branches
  complete, and its text is the single chat reply.

## Shape

```
pipeline (graph):
  strategist ─▶ [ writer , subteam ] ─▶ editor
  subteam (graph): sub_lead ─▶ [ analyst_a , analyst_b ] ─▶ sub_end
```

## Run

```bash
uv run python examples/17_parallel/main.py
```

## Proven by

`tests/e2e/test_complex.py::test_parallel_top_and_nested_batches_all_run` asserts
every leaf (`writer`, `analyst_a`, `analyst_b`, `sub_end`) runs and the `editor`
merge carries the single chat reply. The top-level parallel batch is additionally
covered by `test_compositions.py::test_graph_parallel_batch_all_run`. See the
[Parallel guide](../../docs/workflows/parallel.md).
