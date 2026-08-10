# kaboo-workflows Examples

Each example is a self-contained folder with a `README.md`, `config.yaml`, and `main.py`.

| # | Folder | What it demonstrates |
|---|--------|----------------------|
| 01 | [01_minimal](./01_minimal/) | `load()` in one line — the simplest possible agent |
| 02 | [02_vars_and_anchors](./02_vars_and_anchors/) | Variables & YAML anchors — DRY configuration patterns |
| 03 | [03_tools](./03_tools/) | `tools:` list — auto-loading Python functions as agent tools |
| 04 | [04_session](./04_session/) | `session_manager:` — persistent memory across turns |
| 05 | [05_hooks](./05_hooks/) | `hooks:` — `MaxToolCallsGuard`, `ToolNameSanitizer`, and custom hooks |
| 06 | [06_mcp](./06_mcp/) | MCP — all three connection modes: local server (`mcp_servers:`), external URL (`url:`), stdio (`command:`) |
| 07 | [07_delegate](./07_delegate/) | `mode: delegate` — coordinator routes to specialist agents |
| 08 | [08_swarm](./08_swarm/) | `mode: swarm` — peer agents hand off autonomously |
| 09 | [09_graph](./09_graph/) | `mode: graph` — explicit DAG pipeline between agents |
| 10 | [10_nested](./10_nested/) | Nested orchestration — Swarm inside a Delegate |
| 11 | [11_multi_file_config](./11_multi_file_config/) | Split config across files — infrastructure in one YAML, agents in another |
| 12 | [12_streaming](./12_streaming/) | `wire_event_queue()` — stream every token, tool call, and completion live |
| 13 | [13_graph_conditions](./13_graph_conditions/) | Conditional graph edges — `condition:`, `reset_on_revisit`, `max_node_executions` |
| 14 | [14_agent_factory](./14_agent_factory/) | `type:` + `agent_kwargs:` — custom agent factory instead of default `Agent()` |
| 21 | [21_runtime_configs](./21_runtime_configs/) | `session_config_key` — every run submits its own workflow over a shared base |

### Complex workflows (multi-depth, multi-step)

Each of these ships alongside a [workflow guide](../docs/workflows/) and a passing
end-to-end test that proves its exact behaviour.

| # | Folder | What it demonstrates | Guide |
|---|--------|----------------------|-------|
| 15 | [15_deep_nesting](./15_deep_nesting/) | Delegation nested 3 levels deep | [deep-nesting](../docs/workflows/deep-nesting.md) |
| 16 | [16_swarm_in_graph](./16_swarm_in_graph/) | A swarm and a delegate side by side inside one graph | [swarm-and-graph](../docs/workflows/swarm-and-graph.md) |
| 17 | [17_parallel](./17_parallel/) | Parallel top-level batch + nested parallelism | [parallel](../docs/workflows/parallel.md) |
| 18 | [18_hitl](./18_hitl/) | Nested + parallel human-in-the-loop gates | [human-in-the-loop](../docs/workflows/human-in-the-loop.md) |
| 19 | [19_multiturn_history](./19_multiturn_history/) | Client-driven sub-agent memory across turns | [multi-turn-history](../docs/workflows/multi-turn-history.md) |
| 20 | [20_error_and_rejection](./20_error_and_rejection/) | Isolated errors + safe rejection of gated tools | [errors-and-rejection](../docs/workflows/errors-and-rejection.md) |

## Prerequisites

```bash
uv sync
```

Set `AWS_REGION` and valid Bedrock credentials before running.

The default model is `openai.gpt-oss-20b-1:0`; override per-directory with `MODEL=<model-id>`.

## Running an example

```bash
uv run python examples/01_minimal/main.py
```
