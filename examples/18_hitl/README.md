# 18 — Human-in-the-loop (nested + parallel gates)

> A gated agent deep inside a delegate pauses the whole run to ask the user, then
> resumes with their answer. It can ask several questions at once.

## What this shows

- **`interrupt: true`** grants an agent the built-in `ask_user` tool.
- **Interrupts bubble up** — a gate fired by a nested sub-agent surfaces to the
  single top-level handler and pauses the entire run.
- **Parallel gated tool calls** — an agent can raise multiple gates in one turn
  (e.g. a multi-question form, or two tool calls). Each becomes its own interrupt
  with a distinct tool-call id; the run resumes once all are answered.
- **Rejection is safe** — declining a gate resumes to a clean finish without
  fabricating an approval (see example 20).

## Shape

```
review_team (delegate):
  coordinator ─▶ field_agent (interrupt: true, asks the user)
```

## Prerequisites

- AWS credentials configured (`aws configure` or environment variables)
- Dependencies installed: `uv sync`

## Run

HITL is best experienced through the server + a CopilotKit frontend (the resume
UI lives in the client):

```bash
uv run kaboo-serve examples/18_hitl/config.yaml
```

## Proven by

- `tests/e2e/test_cross_cutting.py::test_ask_user_interrupt_then_resume`
  (parametrized over plain / delegate / swarm / graph positions).
- `tests/e2e/test_complex.py::test_parallel_interrupts_surface_together_and_resume`
  (two gates in one step, distinct ids, both resumed).

See the [Human-in-the-loop guide](../../docs/workflows/human-in-the-loop.md).
