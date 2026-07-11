# 15 — Deep nested delegation (3 levels)

> One top-level entry drives a 3-deep delegate chain — each level is an
> orchestration used as a connection of the level above.

## What this shows

- **Orchestrations as connections** — a delegate connection can target another
  orchestration, not just a plain agent. Nesting them yields arbitrary depth.
- **Topological build order** — `field_team` is built before `research_team`
  before `research_pipeline`, automatically.
- **Transitive activity nesting** — the deepest leaf (`verifier`) surfaces in the
  UI nested under `research_team`, so a user can drill into the whole chain.

## Shape

```
research_pipeline (coordinator)
  └─▶ research_team (lead)
        └─▶ field_team (field_researcher ─▶ verifier)
```

## Run

```bash
uv run python examples/15_deep_nesting/main.py
```

## Proven by

`tests/e2e/test_complex.py::test_delegate_three_levels_of_nesting` drives the
same shape (deterministic scripted models) and asserts `verifier` nests
transitively under `research_team` and the coordinator's reply wraps the chain.
See the [Deep nesting guide](../../docs/workflows/deep-nesting.md).
