# 19 — Multi-turn history

> A delegate sub-agent remembers across turns via client-driven history.

## What this shows

- **`history: true`** on a sub-agent seeds its transcript from
  `state.kaboo_history` on the way in and captures it back out on the way out.
- **The client owns storage** — kaboo enriches the data structure; you decide
  where to persist it. [kaboo-runtime](https://github.com/gl-pgege/kaboo-runtime)
  provides turnkey `ThreadStore` persistence for exactly this.
- **Statelessness is the default** — omit `history:` and sub-agents start fresh
  each turn.

## Shape

```
assistant_team (delegate):
  coordinator ─▶ memo (history: true)
```

## Run

```bash
OPENROUTER_API_KEY=... uv run kaboo-serve examples/19_multiturn_history/config.yaml
```

## Proven by

- `tests/e2e/test_cross_cutting.py::test_subagent_history_round_trips_and_accumulates`
  (turn 2 is seeded from turn 1 and grows).
- `tests/e2e/test_cross_cutting.py::test_history_disabled_sub_agent_does_not_persist`
  (the contrast: no `history:` means no accumulation).

See the [Multi-turn history guide](../../docs/workflows/multi-turn-history.md).
