# Troubleshooting

Fixes for the errors you're most likely to hit first. If something here doesn't
cover your case, check the [Configuration reference](configuration/README.md) or
open an issue.

## Config won't load / validation errors

kaboo-workflows validates the whole config up front and raises a precise error
pointing at the offending key. Common causes:

- **Unknown agent/model reference** — an `agents.*.model` or `entry` names
  something that isn't defined. Names must match a key under `models:` /
  `agents:` exactly.
- **Missing `entry`** — every config needs an `entry` naming the root agent or
  orchestration.
- **Bad tool path** — `tools: [./tools/foo.py]` paths are resolved relative to
  the config file; a missing file or a file with no `@tool`-decorated function
  raises at load time.

Wrap your entrypoint in `cli_errors()` (from `kaboo_workflows`) to get a
formatted, human-readable message instead of a raw traceback.

## Provider / credentials errors

- **`${OPENROUTER_API_KEY}` is empty** — `${VAR}` interpolates from the
  environment. Export it before running (`OPENROUTER_API_KEY=sk-... kaboo-serve
  config.yaml`). Interpolation happens *before* the model is constructed, so a
  missing var usually surfaces as an auth error from the provider.
- **`ModuleNotFoundError` for a provider** — install the matching extra:
  `pip install "kaboo-workflows[openai]"` (also `ollama`, `gemini`,
  `agentcore-memory`).

## MCP lifecycle

- **Server not ready / client can't connect** — the lifecycle starts all
  servers before any client. If you call `load()` yourself, remember it starts
  the lifecycle for you; you are responsible for stopping it.
- **Hanging process on exit** — always stop the lifecycle in a `finally`:

```{.python notest}
from kaboo_workflows import load

resolved = load("config.yaml")
try:
    resolved.entry("hello")
finally:
    resolved.mcp_lifecycle.stop()
```

See the [MCP guide](configuration/Chapter_09.md) for details.

## Serving / CORS

- **Frontend can't reach the server** — `kaboo-serve` listens on
  `http://localhost:8080` (`POST /invocations`, `GET /ping`). Point your
  CopilotKit `runtimeUrl` (or a dev proxy) at it.
- **CORS blocked in the browser** — serve the frontend through the same origin
  or a dev proxy rather than hitting `:8080` cross-origin. The
  [kaboo-workflows-demo](https://github.com/gl-pgege/kaboo-docs/tree/main/examples/kaboo-workflows-demo) shows
  a working proxy setup.

## Nothing streams back

A malformed client transcript (a dangling or duplicate tool result) makes the
model reject the history — an event-less hang. The adapter detects imbalance and
fails fast with a `MALFORMED_HISTORY` error rather than hanging silently; check
that each assistant tool call has exactly one matching tool result.
