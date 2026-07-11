# Getting started

This page takes you from an empty directory to a running multi-agent server in
a few minutes. Every snippet here is exercised in CI.

!!! note "Compatibility"
    kaboo-workflows requires **Python 3.12+** and is built on
    [strands-agents](https://github.com/strands-agents/harness-sdk). Model
    provider SDKs are optional extras (`openai`, `ollama`, `gemini`,
    `agentcore-memory`) — install the one you need.

## 1. Install

```bash
pip install "kaboo-workflows[openai]"
# or with uv
uv add "kaboo-workflows[openai]"
```

## 2. Write a config

The whole system is a single YAML file: models, agents, tools, and the `entry`
point. Save this as `config.yaml`:

```yaml
vars:
  OPENROUTER_API_KEY: ${OPENROUTER_API_KEY}

models:
  default:
    provider: openai
    model_id: anthropic/claude-sonnet-4
    params:
      client_args:
        base_url: https://openrouter.ai/api/v1
        api_key: ${OPENROUTER_API_KEY}

agents:
  assistant:
    model: default
    system_prompt: "You are a helpful assistant."

entry: assistant
```

`${VAR}` interpolates from the environment (see
[Chapter 1](configuration/Chapter_01.md)), so no secrets live in the file.

## 3a. Serve it (AG-UI SSE)

`kaboo-serve` resolves the config and serves your agents as AG-UI
Server-Sent Events that any [CopilotKit](https://copilotkit.ai) frontend can
consume:

```bash
OPENROUTER_API_KEY=sk-... uv run kaboo-serve config.yaml
```

The server listens on `http://localhost:8080` — `POST /invocations` for the
event stream and `GET /ping` for health.

## 3b. Or drive it from Python

`load()` hands back live, fully wired `strands` objects. There are no wrappers:
`resolved.entry` is a real `strands.Agent` (or a `Swarm`/`Graph` for
orchestrations), so you call it directly.

```{.python notest}
from kaboo_workflows import load

resolved = load("config.yaml")
agent = resolved.entry              # a plain strands.Agent
try:
    agent("What is 15 * 23?")       # streams the reply to stdout
finally:
    resolved.mcp_lifecycle.stop()   # always release MCP servers/clients
```

Wrap CLI-style entrypoints in `cli_errors()` for friendly error messages:

```{.python notest}
from kaboo_workflows import cli_errors, load

with cli_errors():
    resolved = load("config.yaml")
    resolved.entry("Hello!")
```

## Next steps

- **[Concepts](concepts.md)** — the mental model behind `load()` and serving.
- **[Configuration reference](configuration/README.md)** — every YAML option,
  chapter by chapter.
- **[Workflows](workflows/deep-nesting.md)** — worked, tested multi-agent
  patterns (delegate, swarm, graph, parallel, HITL).
- **[Examples](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples)** —
  ~20 self-contained, load-tested example projects.
- **[Troubleshooting](troubleshooting.md)** — fixes for the common first-run
  errors.
