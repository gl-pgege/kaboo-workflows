# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
## v0.14.0 (2026-08-09)

### Feat

- **adapters**: runtime-submitted workflow configs — `create_agui_app(session_config_key="workflow_config")` reads each run's own YAML from `forwardedProps` and builds that run's agents, orchestration, entry and MCP client sessions, releasing them when the stream ends. One process now serves many workflows, and editing one takes effect on the next turn with no restart and no file on disk. `allowed_mcp_hosts=` bounds what a submitted config may connect to
- **config**: session overlay merge — `load_session_config(base_raw, overlay)` layers a submitted config over the service's base: `agents` / `orchestrations` / `entry` are replaced, `models` / `mcp_clients` are unioned with the overlay winning a clash, singletons are last-wins, `mcp_servers` and `command:` clients are rejected. The two halves of `load_config` are now separately available as `parse_config_sources` (parse once at boot, keep raw) and `validate_raw_config`
- **runtime**: pending human-in-the-loop interrupts survive a restart with no store to configure — the open gate travels on the AG-UI state channel under `kaboo_session` beside `kaboo_history`, and is restored onto whichever agent serves the resume, including a cold one. On by default; disable with `runtime.persist_session_state: false` when the AG-UI endpoint is exposed straight to a browser
- **mcp**: `MCPLifecycle.start(pin_clients=False)` and `resolve_run_clients(config, infra)` for client sessions owned by a run rather than the process. `MCPClientError: the client session is not running` stops being possible rather than being retried, and `relay` / `obo` auth becomes reachable because the client starts inside the request

### Fix

- **config**: a raw YAML string is no longer probed as a filesystem path. A submitted config over the OS filename limit raised `ENAMETOOLONG`, and one containing a URL was reported as a missing file

### Deprecated

- **runtime**: `runtime.allow_invocation_overrides` and `forwardedProps.agent_config` (`system_prompt` / `model_id`), removed in 0.15.0. A submitted config expresses both, plus the structure they could not reach. Migrating:

  ```python
  # Before — the host projects two fields onto a fixed workflow.
  app = create_agui_app("config.yaml")             # runtime.allow_invocation_overrides: true
  forwarded_props = {"agent_config": {"system_prompt": prompt, "model_id": model}}

  # After — the host submits the workflow.
  app = create_agui_app("config.yaml", session_config_key="workflow_config")
  forwarded_props = {"workflow_config": f"agents:\n  assistant:\n    model: {model}\n    system_prompt: |\n      {prompt}\nentry: assistant"}
  ```

  Keep shared `models:` and `mcp_clients:` in `config.yaml`; the overlay references them by name.

## v0.13.0 (2026-08-02)

### Feat

- **context**: `forwarded_props` is now first-class — the AG-UI request's `forwardedProps` (the host's per-run side channel: run-scoped credentials, tenant context, per-run agent config) is bound per request and readable anywhere via `get_forwarded_props()`; every agent also receives a copy in its state (`ForwardedPropsHook`)
- **attachments**: pluggable, optionally-authorized reference fetching — all attachment URL fetches funnel through `fetch_reference_bytes`; configure `attachments.base_url` / `authorization` (`forwarded_props:<key>` or `env:<VAR>`, applied strictly to own-origin URLs) or register a custom `ReferenceFetcher` via `set_reference_fetcher`; ag-ui-strands' entry-message media fetching is routed through the same funnel (gaining the scheme guard and 25 MB cap)
- **attachments**: `content_url_template` — object references of kind `"attachment"` with a `meta.url` (or a synthesizable one) are upgraded to fetchable attachment transport, so `fetch_attachment` works on every turn, not only the first
- **runtime**: config-gated per-invocation agent overrides — `runtime.allow_invocation_overrides: true` applies `forwardedProps.agent_config` (`system_prompt` / `model_id`) to the executing per-thread agent before each invocation, letting one runtime process serve many host-defined agent types
- **models**: the `openai` provider now defaults `client_args.timeout` to 180s and `max_retries` to 2, so a stalled provider/router connection fails visibly instead of hanging a run forever; user-supplied values always win

## v0.12.0 (2026-07-23)

### Feat

- **hooks**: MCPCallMetaHook — per-call MCP `_meta` stamping (per-request credentials and `toolCallId` correlation on shared MCP clients)
- **hooks**: `interrupt.ttl_seconds` — tool-gate interrupts carry an `expiresAt` ISO-8601 timestamp (AG-UI `Interrupt.expiresAt`) for client countdowns and server-side expiry
- **hooks**: tool-gate approvals accept an edited `tool_input` in the resume payload (AG-UI `approveWithEdits`) — the gated call executes with the user's arguments
- **multiagent**: per-thread interrupt-state isolation on shared Swarm/Graph orchestrators — a gate paused on one conversation is parked per thread and can no longer be observed or clobbered by runs on another; orchestrator runs are serialized (single-flight)

### Fix

- **multiagent**: swarm/graph interrupt descriptors now carry `toolCallId` and `expiresAt` — the mapper is shared with the single-agent path instead of a drifting copy

## v0.11.0 (2026-07-14)

### Feat

- **auth**: pluggable inbound and outbound MCP authentication

## v0.10.0 (2026-07-13)

### Feat

- **references**: configurable attachments & multimodal references

## v0.9.0 (2026-06-22)

### Feat

- **tools**: add serialize_multiagent_result (#62)

### Fix

- **events**: rename error event data key from message to text (#63)

## v0.8.0 (2026-06-22)

### Feat

- **events**: make session id explicit in event stream (#61)

## v0.7.0 (2026-06-21)

### Feat

- **hooks**: include text and message in AGENT_COMPLETE event (#60)

## v0.6.0 (2026-06-20)

### BREAKING CHANGE

- Renamed `complete` event type to` agent_complete`. Any client, integration, or custom hook relying on the `complete` or `COMPLETE` event type must be updated to use `agent_complete` / `AGENT_COMPLETE` instead.

### Fix

- **manifest**: add delegate orchestration entry agent to agents collection

### Refactor

- **events**: rename complete event type to agent_complete

## v0.5.0 (2026-05-24)

### Feat

- **events**: add SESSION_START and SESSION_END lifecycle events (#47)

## v0.4.0 (2026-05-23)

### Feat

- **event_publisher**: Surface agent interrupts as stream events
- **converters**: replace native tool_calls deltas with completed details blocks (#45)

### Fix

- **session-manager**: eliminate session manager double-folder bug (#44)

## v0.3.0 (2026-05-20)

### Feat

- **tools**: preserve full message content across delegation boundary

## v0.2.0 (2026-04-12)

### Feat

- **renderers**: add `typewriter_delay` parameter to `AnsiRenderer` (#28)

## v0.1.2 (2026-03-27)

### Fix

- **tools**: support legacy strands module-based tool pattern (#14)
- add Windows path support (#13)
- align strands-agents version constraint in extras with main dependency (#10)

## v0.1.1 (2026-03-24)

### Fix

- use absolute URL for logo image in README (#8)

## v0.1.0 — 2026-03-23

Initial public release of **kaboo-workflows** — declarative multi-agent orchestration for [strands-agents](https://github.com/strands-agents/harness-sdk).

### Added

- **YAML-first configuration** — define models, agents, tools, hooks, MCP servers, and orchestration topology in a single YAML file
- **Full YAML power** — environment variable interpolation (`${VAR:-default}`), anchors (`&ref` / `*ref`), `x-` scratch-pad keys, and multi-file config merging
- **Multi-model support** — Bedrock, OpenAI, Ollama, Gemini; swap provider with one line
- **MCP servers & clients** — launch local Python servers, connect to remote HTTP endpoints, or spawn stdio subprocesses; lifecycle management with startup ordering, readiness polling, and graceful shutdown
- **Orchestration modes** — Delegate (agent-as-tool), Swarm (peer handoffs), Graph (DAG pipelines) — arbitrarily nestable
- **Event streaming** — unified async event queue across any orchestration depth (tokens, tool calls, handoffs, completions)
- **Session persistence** — file, S3, or Bedrock AgentCore Memory backends; agents remember across restarts
- **Custom agent factories** — plug in your own `Agent` subclass or factory via the `type:` key
- **Hooks** — lifecycle callbacks (`before_invoke`, `after_invoke`, etc.) declared in YAML and implemented in Python
- **`load()` API** — single entry point that resolves, validates, and wires the full agent system; returns plain `strands` objects with no wrappers

### Contributors

- [@galuszkm](https://github.com/galuszkm) — initial design and implementation
