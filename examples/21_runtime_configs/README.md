# 21 — Runtime-submitted workflow configs

One service, a different workflow on every run. No restart, no file on disk.

```
config.yaml          the shared layer: models, mcp_clients, attachments
   │
   └── + forwardedProps.workflow_config   ← the run's own agents/entry
          │
          └── this run's agents, orchestration and MCP client sessions
                 └── released when the run ends
```

## Why

A host that serves many agent types — one per tenant, per customer, per row in
an admin UI — used to need a config file and a restart for each. The workflow
is now something a run *submits*, so the service behaves like a function:
nothing survives a request, and a second replica or a cold instance answers
identically.

## What the overlay does

| Section | Submitted config wins by |
|---------|--------------------------|
| `agents`, `orchestrations`, `entry` | **replacing** the base's outright |
| `models`, `mcp_clients` | **adding to** the base's; a name clash goes to the overlay |
| `runtime`, `attachments`, `log_level` | last-wins |
| `mcp_servers` | rejected — a run may not launch a process |

So the base holds infrastructure and the submitted config holds the workflow.
Neither config in `main.py` declares `models:`; both name the `default` the base
provides. A reference that resolves nowhere fails the run with the available
names listed.

## Serving it

```python
app = create_agui_app(
    "config.yaml",
    session_config_key="workflow_config",
    allowed_mcp_hosts=["mcp.internal"],  # bounds submitted mcp_clients URLs
)
```

Each request reads `forwardedProps["workflow_config"]`, builds that run's
session, and disposes the MCP clients it opened when the stream ends. Omit
`session_config_key` and the process serves one config as before.

`allowed_mcp_hosts` matters because a submitted `mcp_clients:` URL needs no code
to reach a host — unlike a `tools:` import path, which is why the config source
is a trusted-author surface either way.

## What survives between runs

Nothing in the process, and nothing has to. History arrives with each turn on
`state.kaboo_history`, and a pending human-in-the-loop approval arrives on
`state.kaboo_session` — so a rebuilt session is indistinguishable from a
retained one, and an approval outlives the process that opened it. See
[Chapter 7](../../docs/configuration/Chapter_07.md).

## Run it

```bash
uv run python examples/21_runtime_configs/main.py
```

Prints the entry, agents and models each of three runs resolves — one with no
submitted config (the base's fallback agent), one submitting a single triage
agent, one submitting a delegate team.

## See also

- [Chapter 13](../../docs/configuration/Chapter_13.md) — the overlay merge rules
- [Chapter 17](../../docs/configuration/Chapter_17.md) — the two-stage loading pipeline
- [Chapter 09](../../docs/configuration/Chapter_09.md) — per-run MCP client lifetime
