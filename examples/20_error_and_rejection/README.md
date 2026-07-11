# 20 — Errors & rejection

> Two failure modes, both handled safely: an isolated mid-run error, and a user
> declining a gated action.

## What this shows

- **Error isolation** — if a node raises mid-run, the failure is isolated to that
  node (earlier nodes stay `completed`, the failing node's card is marked
  `error`) and the run *terminates* rather than hanging.
- **Rejection** — the same gate mechanism as [example 18](../18_hitl/): declining
  a gated tool feeds the tool a cancellation sentinel ("The user declined to
  answer.") and the agent proceeds without fabricating an approval; the run
  finishes cleanly.

## Shape

```
review_swarm (swarm): planner ─▶ researcher ─▶ writer
```

## Run

```bash
uv run python examples/20_error_and_rejection/main.py
```

## Proven by

- `tests/e2e/test_cross_cutting.py::test_swarm_node_error_is_isolated_and_run_terminates`
  (earlier node completed, failing node marked error, run terminates).
- `tests/e2e/test_complex.py::test_rejected_interrupt_finishes_cleanly`
  (a declined gate resumes to a clean RUN_FINISHED).

See the [Errors & rejection guide](../../docs/workflows/errors-and-rejection.md).
