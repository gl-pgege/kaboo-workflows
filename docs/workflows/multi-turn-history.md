# Multi-turn history

A delegate sub-agent starts fresh every turn by default. Set `history: true` and
its transcript is seeded from `state.kaboo_history` on the way in and captured
back into the outgoing snapshot on the way out — so it can remember across turns.

```
assistant_team (delegate):
  coordinator ─▶ memo (history: true)
```

## Config

```yaml
agents:
  memo:
    history: true
    system_prompt: "Use the notes from previous turns when answering follow-ups."

orchestrations:
  assistant_team:
    mode: delegate
    entry_name: coordinator
    connections:
      - { agent: memo, description: "Records and recalls notes across turns" }

entry: assistant_team
```

Full runnable project: [`examples/19_multiturn_history`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/19_multiturn_history).

## The client owns storage

kaboo enriches the data structure; it does **not** choose your datastore. The
captured transcript comes back in the run's outgoing history snapshot, and the
client feeds it back in `state.kaboo_history` on the next turn. For turnkey
persistence (in-memory or Postgres `ThreadStore`s behind a CopilotKit runtime),
use [kaboo-runtime](https://github.com/gl-pgege/kaboo-runtime).

## Run

```bash
OPENROUTER_API_KEY=... uv run kaboo-serve examples/19_multiturn_history/config.yaml
```

## Proven by

- `tests/e2e/test_cross_cutting.py::test_subagent_history_round_trips_and_accumulates`
  (turn 2 is seeded from turn 1 and grows).
- `tests/e2e/test_cross_cutting.py::test_history_disabled_sub_agent_does_not_persist`
  (the stateless-by-default contrast).
