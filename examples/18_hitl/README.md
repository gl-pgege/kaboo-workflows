# 18 — Human-in-the-loop: a delegated agent asks the person

> An agent deep inside a delegate asks the person a question through the
> browser, then carries on with the answer.

## What this shows

- **`client_tools: [ask_user]`** lets an agent call the browser's question tool
  (kaboo-react's `useAskUserTool`). Any agent may, delegates included.
- **The question reaches the browser from any depth** — the run ends with the
  call pending, and the browser's answer goes back to the agent that asked.
- **The coordinator hears it too** — the delegate's result tells it what was
  asked and answered.

## Shape

```
review_team (delegate):
  coordinator ─▶ field_agent (client_tools: [ask_user])
```

## Prerequisites

- AWS credentials configured (`aws configure` or environment variables)
- Dependencies installed: `uv sync`

## Run

Serve it and connect a kaboo-react frontend that registers `useAskUserTool()`:

```bash
uv run kaboo-serve examples/18_hitl/config.yaml
```

## Proven by

- `tests/e2e/test_client_tools.py` (an entry agent's question, a delegate's,
  one two delegations down, and one answered in another process).

See the [Human-in-the-loop guide](../../docs/workflows/human-in-the-loop.md).
