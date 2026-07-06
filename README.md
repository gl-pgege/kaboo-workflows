# kaboo-workflows

**YAML-driven multi-agent orchestration with AG-UI and CopilotKit support, built on [strands-agents](https://github.com/strands-agents/harness-sdk)**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

> Forked from [strands-compose](https://github.com/strands-compose/sdk-python). Extended with native AG-UI protocol support for CopilotKit frontends and AgentCore deployment.

---

## Quick Start

### 1. Install

```bash
pip install kaboo-workflows[openai]
# or with uv
uv add kaboo-workflows[openai]
```

Extras: `openai`, `ollama`, `gemini`, `agentcore-memory`

### 2. Create a config

```yaml
# config.yaml
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
    tools:
      - ./tools/calculator.py

entry: assistant
```

### 3. Create a tool

```python
# tools/calculator.py
from strands.tools.decorator import tool

@tool
def calculator(expression: str) -> str:
    """Evaluate a math expression and return the result."""
    import ast
    result = eval(compile(ast.parse(expression, mode='eval'), '<expr>', 'eval'))
    return f"Result: {result}"
```

### 4. Start the server

```bash
OPENROUTER_API_KEY=sk-... uv run kaboo-serve config.yaml
```

That's it. Your agent is now serving AG-UI SSE on `http://localhost:8080/invocations`.

### 5. Test it

```bash
# Health check
curl http://localhost:8080/ping

# Send a message
curl -s -N -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{
    "thread_id": "thread-1",
    "run_id": "run-1",
    "messages": [{"id": "msg-1", "role": "user", "content": "What is 15 * 23?"}],
    "tools": [],
    "context": [],
    "state": {},
    "forwarded_props": {}
  }'
```

You'll see AG-UI events stream back:

```
RUN_STARTED → TEXT_MESSAGE_START → TEXT_MESSAGE_CONTENT →
TOOL_CALL_START → TOOL_CALL_ARGS → TOOL_CALL_END → TOOL_CALL_RESULT →
TEXT_MESSAGE_CONTENT → TEXT_MESSAGE_END → RUN_FINISHED
```

---

## How It Works

kaboo-workflows does three things:

1. **YAML → Agents**: Your config defines models, agents, tools, hooks, MCP servers, and orchestrations. `load()` resolves everything into live strands objects.
2. **Agents → AG-UI SSE**: `kaboo-serve` wraps the resolved agents with [ag-ui-strands](https://github.com/ag-ui-protocol/ag-ui/tree/main/integrations/aws-strands/python) and serves them as AG-UI Server-Sent Events.
3. **AG-UI → CopilotKit**: Any CopilotKit frontend connects directly. Generative UI, shared state, and human-in-the-loop work out of the box.

```
YAML config → load() → strands.Agent → StrandsAgent (AG-UI) → FastAPI SSE
                                                                    ↑
Browser → CopilotKit Runtime ──────────────────────────────────────┘
```

### AgentCore Deployment

kaboo-workflows is fully compatible with [Amazon Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-agui.html). Deploy with the AG-UI protocol flag:

```bash
agentcore configure -e my_server.py --protocol AGUI
agentcore deploy
```

AgentCore handles auth, session isolation, and scaling. Your `kaboo-serve` server serves the same `/invocations` (AG-UI SSE) and `/ping` endpoints that AgentCore expects.

---

## Model Providers

Swap providers by changing `models.default` in your YAML:

```yaml
# OpenRouter (any model via OpenAI-compatible API)
default:
  provider: openai
  model_id: anthropic/claude-sonnet-4
  params:
    client_args:
      base_url: https://openrouter.ai/api/v1
      api_key: ${OPENROUTER_API_KEY}

# AWS Bedrock
default:
  provider: bedrock
  model_id: us.anthropic.claude-sonnet-4-6-v1:0

# Local Ollama
default:
  provider: ollama
  model_id: llama3

# Direct OpenAI
default:
  provider: openai
  model_id: gpt-4o
```

---

## Multi-Agent Orchestration

Three orchestration modes, arbitrarily nestable:

### Delegate — agent as a tool

```yaml
orchestrations:
  team:
    mode: delegate
    entry_name: coordinator
    connections:
      - agent: researcher
        description: "Research the topic."
      - agent: writer
        description: "Write the report."
entry: team
```

### Swarm — autonomous handoffs

```yaml
orchestrations:
  review:
    mode: swarm
    entry_name: drafter
    agents: [drafter, reviewer, tech_lead]
    max_handoffs: 10
entry: review
```

### Graph — deterministic DAG

```yaml
orchestrations:
  pipeline:
    mode: graph
    entry_name: writer
    edges:
      - from: writer
        to: reviewer
      - from: reviewer
        to: publisher
entry: pipeline
```

---

## CLI Reference

| Command | What it does |
|---------|-------------|
| `uv run kaboo-serve config.yaml` | Start AG-UI SSE server (port 8080) |
| `uv run kaboo-serve config.yaml --port 9000` | Custom port |
| `uv run kaboo-workflows check config.yaml` | Validate config (no side-effects) |
| `uv run kaboo-workflows load config.yaml` | Full load + MCP health check |

---

## Using as a Library

```python
from kaboo_workflows import load
from kaboo_workflows.adapters import create_agui_app

# Option 1: Programmatic agent use
resolved = load("config.yaml")
result = resolved.entry("Hello!")

# Option 2: Create a FastAPI app for custom middleware
app = create_agui_app("config.yaml")
```

---

## Examples

```bash
# Serve any example
OPENROUTER_API_KEY=... uv run kaboo-serve examples/step1/config.yaml

# Or run as a REPL
OPENROUTER_API_KEY=... uv run python examples/step1/main.py
```

See [examples/](examples/) for the full list.

---

## Developer Setup

```bash
git clone https://github.com/gl-pgege/kaboo-workflows.git
cd kaboo-workflows
uv sync --all-extras

uv run just check        # lint + type check + security scan
uv run just test         # pytest with coverage (221 tests)
uv run just format       # auto-format
```

---

## Attribution

Forked from [strands-compose/sdk-python](https://github.com/strands-compose/sdk-python) (Apache 2.0). Original work by [Michal Galuszka](https://github.com/strands-compose).
