# Chapter 13: Multi-File Configs — Splitting and Merging

[← Back to Table of Contents](README.md) | [← Previous: Nested Orchestrations](Chapter_12.md)

---

Large configs can be split across multiple files. Pass a list to `load()` and they're merged:

```{.python notest}
from kaboo_workflows import load

resolved = load(["base.yaml", "agents.yaml", "mcp.yaml"])
```

## Merge Rules

**Collection sections** (dicts) are merged across files:

- `models` — merged
- `agents` — merged
- `orchestrations` — merged
- `mcp_servers` — merged
- `mcp_clients` — merged

**Singleton fields** use last-wins semantics:

- `entry` — last file's value wins
- `session_manager` — last file's value wins
- `log_level` — last file's value wins
- `version` — last file's value wins

## Duplicate Detection

If two files define the same name in the same collection section, loading fails:

```yaml
# file_a.yaml
agents:
  helper:
    system_prompt: "I help."

# file_b.yaml
agents:
  helper:                  # Duplicate!
    system_prompt: "I also help."
```

```
ValueError: Duplicate names in 'agents' across config sources: ['helper']
```

## Per-File Variable Interpolation

Each file's `vars` block is interpolated independently *before* merging. This means File A's vars don't affect File B:

```yaml
# base.yaml
vars:
  MODEL: us.anthropic.claude-sonnet-4-6-v1:0
models:
  default:
    provider: bedrock
    model_id: ${MODEL}         # Resolves from base.yaml's vars

# agents.yaml
vars:
  TONE: friendly               # This is agents.yaml's own vars
agents:
  assistant:
    model: default
    system_prompt: "You are ${TONE}."
entry: assistant
```

## Typical Split Patterns

**Infrastructure + Application**:
```
base.yaml     — vars, models, mcp_servers, mcp_clients, session_manager
agents.yaml   — agents, orchestrations, entry
```

**Environment Layering**:
```
base.yaml        — shared models, shared agents
production.yaml  — production model IDs, production entry
staging.yaml     — staging model IDs, staging entry
```

**Team-Based**:
```
models.yaml       — all model definitions
research.yaml     — researcher agents + orchestrations
content.yaml      — writer/editor agents + orchestrations
main.yaml         — coordinator agent, top-level orchestration, entry
```

## Neither File Needs to Be Complete

Individual files don't need to be valid on their own. `base.yaml` can define models without agents or entry. `agents.yaml` can reference models it doesn't define. The merged result must be valid — individual files don't.

## The Second Merge Mode: Session Overlays

Everything above describes **composition**: several files that together are one config, which is why a duplicate name is an error rather than an override.

A server whose runs bring their own workflow needs the opposite — **substitution**. That is `load_session_config(base_raw, overlay)`, and it merges by different rules:

| Section | Multi-file merge | Session overlay |
|---------|------------------|-----------------|
| `agents`, `orchestrations`, `entry` | merged; duplicate name is an error | **replaced** wholesale by the overlay |
| `models`, `mcp_clients` | merged; duplicate name is an error | **unioned**, and the overlay wins a name clash |
| `runtime`, `attachments`, `log_level`, `session_manager` | last-wins | last-wins from the overlay |
| `mcp_servers` | merged | **rejected** in an overlay |

Replace rather than merge is what makes a submitted config a whole workflow instead of an addition to someone else's. Union on `models` and `mcp_clients` is what makes the base useful: it holds the shared infrastructure every workflow draws on by name, and an overlay may still add its own.

```{.python notest}
from kaboo_workflows import load_session_config, parse_config_sources

base_raw = parse_config_sources("config.yaml")   # once, at boot

config = load_session_config(base_raw, submitted_yaml)  # per run
```

`base_raw` is never mutated, so one parse safely serves concurrent runs. The overlay is interpolated as its own source, so a `${OPENROUTER_KEY}` in a config authored in your database resolves from the serving process's environment — the secret stays where it belongs.

Two things an overlay may not do, because a submitted config is data from somewhere else even when its author is trusted:

- `mcp_servers:` is rejected outright. Starting a server means running a command.
- `mcp_clients:` with `command:` is rejected, and a `url:` is checked against `allowed_mcp_hosts` when you pass one. Unlike a Python import path, a URL needs no code on your machine to send your agent's context somewhere else.

Superset enforcement comes free from validation you already have: an overlay agent naming a client nobody defined fails with `Agent 'x' references MCP client 'y' which is not defined. Available: [...]`.

See [Chapter 17](Chapter_17.md) for how this fits the loading pipeline, `create_agui_app(session_config_key=...)` for the serving side, and [`examples/21_runtime_configs`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/21_runtime_configs) for a base and two submitted workflows you can run.

> **Tips & Tricks**
>
> - Use multi-file configs when your single file exceeds ~200 lines. It makes diffs cleaner and team collaboration easier.
> - The `entry` field should typically go in the "application" file, not the "infrastructure" file — it's the most likely to change between use cases.
> - File paths for tools/hooks/servers are resolved relative to the file they appear in. If `agents.yaml` says `tools: [./tools.py]`, it looks for `tools.py` next to `agents.yaml`.

---

[Next: Chapter 14 — Agent Factories →](Chapter_14.md)
