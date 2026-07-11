# Errors & rejection

Two "unhappy path" behaviours, both handled safely.

## 1. Error isolation

If a node raises mid-run, the failure is isolated to that node: earlier nodes
stay `completed`, the failing node's activity card is marked `error`, and the run
**terminates** rather than hanging.

```
review_swarm (swarm): planner ─▶ researcher ─▶ writer
```

```yaml
orchestrations:
  review_swarm:
    mode: swarm
    agents: [planner, researcher, writer]
    entry_name: planner

entry: review_swarm
```

Full runnable project: [`examples/20_error_and_rejection`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/20_error_and_rejection).

### Proven by

`tests/e2e/test_cross_cutting.py::test_swarm_node_error_is_isolated_and_run_terminates`
— the earlier node stays completed, the failing node is marked `error`, and the
run terminates (never stalls).

## 2. Rejection

Rejection uses the same gate mechanism as [human-in-the-loop](human-in-the-loop.md).
When the client resumes an interrupt with `{"status": "cancelled"}` (or simply
does not address it), the gated tool receives a cancellation sentinel ("The user
declined to answer.") and the agent proceeds **without fabricating an approval**.
kaboo never silently approves an unaddressed interrupt — the safe default is to
cancel.

```json
// resume payload declining the gate
[{ "interruptId": "<id>", "status": "cancelled" }]
```

### Proven by

`tests/e2e/test_complex.py::test_rejected_interrupt_finishes_cleanly` — a declined
gate resumes to a clean `RUN_FINISHED` with no error and no hang.
