# Chapter 15: Event Streaming — Real-Time Observability

[← Back to Table of Contents](README.md) | [← Previous: Agent Factories](Chapter_14.md)

---

When you have nested orchestrations running, you need visibility into what's happening. `wire_event_queue()` attaches event publishers to every agent and orchestrator and funnels all events into a single async queue.

```{.python notest}
import asyncio
from kaboo_workflows import AnsiRenderer, load

async def main():
    resolved = load("config.yaml")
    queue = resolved.wire_event_queue()

    async def invoke():
        try:
            await resolved.entry.invoke_async("Analyze LLM trends.")
        finally:
            await queue.close()

    asyncio.create_task(invoke())

    renderer = AnsiRenderer()
    while (event := await queue.get()) is not None:
        renderer.render(event)
    renderer.flush()

asyncio.run(main())
```

## Event Types

Every event is a `StreamEvent` dataclass with four fields: `type`, `agent_name`, `timestamp`, and `data`.

### Session lifecycle events

These two events bracket every invocation. They are produced by the queue layer, not by individual agents.

| Event Type | Description | `data` payload |
|------------|-------------|----------------|
| `SESSION_START` | First event on the queue — emitted before any agent activity | `{"session_id": "<id or null>", "manifest": {SessionManifest}}` — agents, orchestrations, entry point, model info, session manager locations |
| `SESSION_END` | Last typed event before the stream closes | `{"session_id": "<id or null>"}` |

The `SESSION_START` payload wraps the full wired topology snapshot together with the effective session id. Use the `manifest` key to restore conversation history, render an architecture diagram, or audit which models are in use — before any agent has run.

### Per-agent events

| Event Type | Description |
|------------|-------------|
| `AGENT_START` | Agent begins processing |
| `TOKEN` | Individual token streamed from LLM |
| `REASONING` | Reasoning/thinking content from LLM |
| `TOOL_START` | Tool execution begins |
| `TOOL_END` | Tool execution completes |
| `INTERRUPT` | Agent pauses for human input |
| `AGENT_COMPLETE` | Agent finishes — `data` carries `usage` metrics, `text` (final output string), and `message` (raw message dict) |
| `ERROR` | Model or execution error — `data` carries `text` and `exception_type` |

`AGENT_COMPLETE` is not guaranteed on every finish. When a run stops because an
agent raised an interrupt, `INTERRUPT` is emitted and the publisher returns
without a completion event, so a consumer that waits for `AGENT_COMPLETE` before
releasing a turn will hang on any human-in-the-loop pause. Treat `INTERRUPT` as
an equally valid end of turn.

### Stream group events

| Event Type | Description |
|------------|-------------|
| `STREAM_GROUP_START` | An agent in a `stream.group` begins; `data` carries `parent_group`, `tool_call_id` and the `task` it was handed |
| `STREAM_GROUP_END` | That group finishes |

Once an agent is in a group, **every** event it emits also carries
`stream_group` and `stream_title` in `data`, so a consumer can route events to
the right activity card without tracking the start event. A grouped agent that
sets no `title` gets one derived from its name — `data_analyst` becomes
`Data Analyst`.

### Multi-agent events

| Event Type | Description |
|------------|-------------|
| `NODE_START` | Graph/swarm node begins |
| `NODE_STOP` | Graph/swarm node completes |
| `HANDOFF` | Swarm agent hands off to another |
| `MULTIAGENT_START` | Multi-agent orchestration begins |
| `MULTIAGENT_COMPLETE` | Multi-agent orchestration completes |

## AnsiRenderer

The built-in `AnsiRenderer` prints colored terminal output — agent names, tool calls, reasoning traces, tokens — all streaming live. Perfect for development and debugging.

## Custom Event Consumers

Events are `StreamEvent` dataclasses with `.asdict()` for serialization:

```{.python notest}
while (event := await queue.get()) is not None:
    data = event.asdict()
    # Send to websocket, log to file, push to metrics system...
```

A typical consumer pattern that handles the session lifecycle:

```{.python notest}
while (event := await queue.get()) is not None:
    if event.type == "session_start":
        session_id = event.data.get("session_id")
        manifest = event.data["manifest"]  # full topology snapshot
        entry = manifest["entry"]          # {"name": "...", "kind": "agent|orchestration"}
    elif event.type == "session_end":
        session_id = event.data.get("session_id")
    else:
        # per-agent or multi-agent event
        process(event)
```

## What Is Configured Where

The **queue** is a Python concern: you create it and call `wire_event_queue()`,
and there is no YAML for it. The hooks that fills in (`EventPublisher`) listen to
the same lifecycle events as your YAML-defined hooks, and the two coexist.

How an agent's output is **labelled** in that stream, though, is YAML:

```yaml
agents:
  researcher:
    model: default
    stream:
      group: research      # groups this agent's events with others in `research`
      title: Researching    # human-readable label for the group
```

`stream:` takes `group` and `title`, both optional. Agents sharing a `group`
bracket their combined output with `STREAM_GROUP_START` and `STREAM_GROUP_END`
events, which is how a UI renders several agents' work as one collapsible
activity rather than an interleaved mess.

> **Tips & Tricks**
>
> - Call `wire_event_queue()` only **once** per `ResolvedConfig` — it mutates agents and orchestrators by adding hooks. Calling it twice would double-attach publishers.
> - Call `queue.flush()` between requests to clear stale events from a previous invocation. This also resets the `SESSION_START` / `SESSION_END` guards so the next cycle can re-emit them.
> - The queue has a max size of 10,000. If your agent generates more events than the consumer processes, events are dropped with a warning.
> - `SESSION_START` is emitted synchronously by `wire_event_queue()` before any agent runs. `SESSION_END` is emitted by `queue.close()` — always call it in a `finally` block.

---

[Next: Chapter 16 — Name Sanitization →](Chapter_16.md)
