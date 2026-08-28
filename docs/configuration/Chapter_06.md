# Chapter 6: Hooks — Middleware for Agents

[← Back to Table of Contents](README.md) | [← Previous: Tools](Chapter_05.md)

---

Hooks are lifecycle callbacks that fire at specific points during agent execution — before/after invocations, before/after tool calls, before/after model calls. They're perfect for guardrails, logging, metrics, and custom behavior.

```yaml
agents:
  assistant:
    model: default
    hooks:
      - type: kaboo_workflows.hooks:MaxToolCallsGuard
        params:
          max_calls: 10
      - type: kaboo_workflows.hooks:ToolNameSanitizer
      - type: ./my_hooks.py:AuditLogger
        params:
          log_file: ./audit.log
    system_prompt: "You are a helpful assistant."
```

## Hook Specification Formats

Hooks can be specified in two ways:

**Inline object** — with `type` and optional `params`:

```yaml
hooks:
  - type: kaboo_workflows.hooks:MaxToolCallsGuard
    params:
      max_calls: 10
```

**String shorthand** — just the import path (no params):

```yaml
hooks:
  - kaboo_workflows.hooks:ToolNameSanitizer
```

Both the `type` field and the string shorthand accept:
- `module.path:ClassName` — for installed packages
- `./file.py:ClassName` — for local files (relative to config file)

## Built-in Hooks

kaboo-workflows exports eleven hook classes, but most are wired up for you when you enable
the feature they serve — `HistoryHook`, `InterruptHook`, `ReferenceHook`, `SessionStateHook`,
`ForwardedPropsHook`, `EventPublisher` and `MultiAgentStopGuard` all appear because of some
other config key, not because you listed them. These four are the ones you add by hand:

### `MaxToolCallsGuard`

Limits how many tool calls an agent can make in a single invocation. Two-phase behavior:

`max_calls` defaults to **25**. Two-phase behaviour:

1. **First violation** — injects a system message telling the LLM to stop and write a final answer.
2. **Second violation** — if the LLM ignores the warning and calls another tool, the loop is terminated.

```yaml
hooks:
  - type: kaboo_workflows.hooks:MaxToolCallsGuard
    params:
      max_calls: 15
```

### `ToolNameSanitizer`

Some models inject extra tokens into tool names (e.g., `search<|python_tag|>` instead of `search`). This hook strips those artifacts so strands can find the tool in the registry.

```yaml
hooks:
  - type: kaboo_workflows.hooks:ToolNameSanitizer
```

No params needed — just add it.

### `MCPCallMetaHook`

Attaches per-call metadata (MCP `params._meta`) to every MCP tool call the agent makes. The metadata is computed on `BeforeToolCallEvent` — in the **caller's request context**, where per-request contextvars (the inbound `Principal`, application-bound run state) are visible — and rides the JSON-RPC message itself.

Use this when a long-lived shared MCP client must carry a *per-request* credential or correlation id: header-based auth strategies (see the `auth:` field in [Chapter 9](Chapter_09.md)) resolve on the transport's writer task, whose context was snapshotted once at client start, so they cannot see per-request state on a shared client. `_meta` can, because it is bound at call time.

```yaml
agents:
  assistant:
    mcp: [platform]
    hooks:
      - type: kaboo_workflows.hooks:MCPCallMetaHook
        params:
          provider: ./mcp/auth.py:provide_call_meta   # (tool_use) -> dict | None
```

The provider is a callable `(tool_use) -> dict | None` (or an import spec resolving to one). Its result is merged into `_meta`; unless the provider set one, `toolCallId` is stamped with the strands `toolUseId`, giving the server a stable idempotency/correlation key per tool invocation. Omit `provider` to stamp only `toolCallId`. Non-MCP tools are untouched.

### `StopGuard`

A cooperative stop mechanism — set a flag on the guard and the agent stops cleanly at the next opportunity. Useful for external cancellation (e.g., user disconnects from a web socket).

`StopGuard` needs a Python callable for `stop_check`, so it's usually wired from Python rather than pure YAML:

```python
from kaboo_workflows.hooks import stop_guard_from_event

guard, stop = stop_guard_from_event()

# add `guard` to an agent's hooks, then later:
stop.set()
```

## Writing Custom Hooks

A hook is any class that subclasses `strands.hooks.HookProvider` and implements `register_hooks()`:

```python
# my_hooks.py
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterToolCallEvent, AfterInvocationEvent

class ToolCounter(HookProvider):
    """Counts tool calls and prints a summary after each invocation."""

    def __init__(self, verbose: bool = False):
        self._count = 0
        self._verbose = verbose

    def register_hooks(self, registry: HookRegistry, **kwargs):
        registry.add_callback(AfterToolCallEvent, self._on_tool)
        registry.add_callback(AfterInvocationEvent, self._on_done)

    def _on_tool(self, event: AfterToolCallEvent):
        self._count += 1
        if self._verbose:
            print(f"  Tool #{self._count}: {event.tool_use.get('name')}")

    def _on_done(self, event: AfterInvocationEvent):
        print(f"Agent used {self._count} tools this turn.")
        self._count = 0
```

Use it in YAML:

```yaml
hooks:
  - type: ./my_hooks.py:ToolCounter
    params:
      verbose: true
```

The `params` dict is spread as `**kwargs` to your class constructor.

## Hook Execution Order

Hooks fire in the order they're listed. First hook's callbacks run before the second hook's for the same event. This matters when hooks interact — for example, `ToolNameSanitizer` should run before hooks that inspect tool names.

## Available Hook Events

These are the strands lifecycle events you can listen to:

| Event | When It Fires |
|-------|---------------|
| `BeforeInvocationEvent` | Before the agent starts processing |
| `AfterInvocationEvent` | After the agent finishes |
| `BeforeModelCallEvent` | Before each LLM API call |
| `AfterModelCallEvent` | After each LLM API call |
| `BeforeToolCallEvent` | Before each tool execution |
| `AfterToolCallEvent` | After each tool execution |
| `BeforeNodeCallEvent` | Before a graph/swarm node executes |
| `AfterNodeCallEvent` | After a graph/swarm node executes |
| `BeforeMultiAgentInvocationEvent` | Before a multi-agent orchestration starts |
| `AfterMultiAgentInvocationEvent` | After a multi-agent orchestration completes |

## Hooks on Orchestrations

Orchestrations also support hooks — applied at the orchestration level, not the individual agent level:

```yaml
orchestrations:
  pipeline:
    mode: graph
    entry_name: writer
    hooks:
      - type: kaboo_workflows.hooks:MaxToolCallsGuard
        params: { max_calls: 30 }
    edges:
      - from: writer
        to: reviewer
```

> **Tips & Tricks**
>
> - Each agent gets **fresh hook instances**. Two agents with the same hook config get independent instances — no shared state between them.
> - `params` preserves YAML types. `{ max_calls: 15 }` passes `max_calls` as an integer, `{ log_file: ./out.log }` as a string.
> - Hooks are the right place for cross-cutting concerns: rate limiting, audit logging, cost tracking, safety guardrails.
> - Combine `MaxToolCallsGuard` and `ToolNameSanitizer` as a baseline for any agent that uses tools — they handle the most common edge cases.

---

[Next: Chapter 7 — Session Persistence →](Chapter_07.md)
