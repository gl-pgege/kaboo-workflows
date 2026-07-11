# Deep nested delegation (3+ levels)

A delegate **connection** can target another orchestration, not just a plain
agent. Nesting orchestrations this way gives you arbitrary depth from a single
top-level entry.

```
research_pipeline (coordinator)
  └─▶ research_team (lead)
        └─▶ field_team (field_researcher ─▶ verifier)
```

## Config

Each level is a delegate whose connection is the orchestration below it.
kaboo-workflows topologically sorts them, building `field_team` first.

```yaml
orchestrations:
  field_team:
    mode: delegate
    entry_name: field_researcher
    connections:
      - { agent: verifier, description: "Double-check the collected data" }
  research_team:
    mode: delegate
    entry_name: lead
    connections:
      - { agent: field_team, description: "Collect and verify field data" }
  research_pipeline:
    mode: delegate
    entry_name: coordinator
    connections:
      - { agent: research_team, description: "Research the topic end to end" }

entry: research_pipeline
```

Full runnable project: [`examples/15_deep_nesting`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/15_deep_nesting).

## Run

```bash
uv run python examples/15_deep_nesting/main.py
```

## What you'll observe

- The top entry (`coordinator`) runs *as* the orchestration and owns the reply,
  wrapping the whole chain.
- The deepest leaf (`verifier`) surfaces in the activity tree nested transitively
  under `research_team` — so a user can drill from the coordinator down to the
  verifier.

## Proven by

`tests/e2e/test_complex.py::test_delegate_three_levels_of_nesting` drives the same
shape with deterministic scripted models and asserts the transitive nesting and
the coordinator's wrapping reply.
