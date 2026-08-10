# API reference

A curated map of the public surface, grouped by import path. Every module below
has a full, auto-generated reference (rendered from docstrings by mkdocstrings) —
follow the links for signatures, parameters, and source.

The public surface is guarded by `tests/contract/test_public_api.py`: every
symbol in a public `__all__` must resolve, carry a docstring, and appear on one
of these pages.

## Top level — [`kaboo_workflows`](api/kaboo_workflows.md)

The curated top-level exports, for the common case:

- **Config pipeline**: `load`, `load_config`, `load_session`, `resolve_infra`,
  `AppConfig`, `ConfigInput`, `ResolvedConfig`, `ResolvedInfra`
- **Per-run configs**: `parse_config_sources`, `validate_raw_config`,
  `load_session_config`, `resolve_run_clients` — the two-stage pipeline for a
  service whose runs submit their own workflow
  ([Chapter 17](configuration/Chapter_17.md#when-the-config-itself-is-per-request))
- **Orchestration**: `OrchestrationBuilder`
- **Streaming**: `make_event_queue`, `EventQueue`, `StreamEvent`, `EventType`
- **Tools**: `node_as_tool`, `node_as_async_tool`, `serialize_multiagent_result`
- **MCP**: `create_mcp_client`, `create_mcp_server`, `MCPLifecycle`
- **Hooks**: `EventPublisher`, `MaxToolCallsGuard`, `StopGuard`, `ToolNameSanitizer`
- **Rendering / CLI**: `AnsiRenderer`, `cli_errors`
- **Errors**: `ConfigurationError`, `SchemaValidationError`,
  `UnresolvedReferenceError`, `CircularDependencyError`,
  `ImportResolutionError` (see `kaboo_workflows.exceptions`)

## Serving — [`kaboo_workflows.adapters`](api/adapters.md)

- `create_agui_app` — turn a config into a FastAPI AG-UI / CopilotKit app. This is
  the primary serving entrypoint and lives here (not re-exported top level).
  `session_config_key=` makes each run bring its own config;
  `allowed_mcp_hosts=` bounds what a submitted config may connect to.

## Configuration — [`kaboo_workflows.config`](api/config.md)

Loaders, interpolation, infra resolution, and the full YAML schema models
(`AgentDef`, `ModelDef`, `OrchestrationDef`, `DelegateOrchestrationDef`,
`SwarmOrchestrationDef`, `GraphOrchestrationDef`, `MCPServerDef`, `MCPClientDef`,
`HookDef`, `SessionManagerDef`, …).

## Hooks — [`kaboo_workflows.hooks`](api/hooks.md)

`EventPublisher`, `HistoryHook`, `InterruptHook`, `MaxToolCallsGuard`,
`MultiAgentStopGuard`, `SessionStateHook`, `StopGuard`, `ToolNameSanitizer`,
`restore_session_state`, `session_state_snapshot`, `stop_guard_from_event`.

## MCP — [`kaboo_workflows.mcp`](api/mcp.md)

`MCPClient`, `MCPServer`, `MCPLifecycle`, `create_mcp_client`,
`create_mcp_server`, and the `sse` / `stdio` / `streamable_http` transports.

## Tools — [`kaboo_workflows.tools`](api/tools.md)

`ask_user`, the `load_tool*` discovery helpers, `node_as_tool` /
`node_as_async_tool`, `resolve_tool_spec(s)`, `serialize_multiagent_result`.

## Converters — [`kaboo_workflows.converters`](api/converters.md)

`StreamConverter`, `OpenAIStreamConverter`, `RawStreamConverter`.

## Renderers — [`kaboo_workflows.renderers`](api/renderers.md)

`AnsiRenderer`.

## Types — [`kaboo_workflows.types`](api/types.md)

`EventType`, `StreamEvent`, and the `SessionManifest` family
(`AgentDescriptor`, `OrchestrationDescriptor`, `EntryDescriptor`,
`ModelDescriptor`, `NodeRef`, `EdgeRef`).
