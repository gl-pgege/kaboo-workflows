# Chapter 18: Full Reference — Every Field at a Glance

[← Back to Table of Contents](README.md) | [← Previous: The Loading Pipeline](Chapter_17.md)

---

## Root Config

```yaml
version: "1"          # Optional, defaults to "1"
vars: {}              # Variable definitions (removed after interpolation)
models: {}            # Named model definitions
agents: {}            # Named agent definitions (required: at least one)
orchestrations: {}    # Named orchestration definitions
mcp_servers: {}       # Named MCP server definitions
mcp_clients: {}       # Named MCP client connections
session_manager: {}   # Global session manager
history: false        # Default for per-agent history when an agent omits history:
attachments: {}       # Global reference/attachment policy (AttachmentsDef)
runtime: {}           # Runtime behavior toggles (RuntimeDef)
telemetry: {}         # OpenTelemetry export (TelemetryDef), off by default
entry: "name"         # Required: entry point agent or orchestration
log_level: "WARNING"  # Optional: DEBUG, INFO, WARNING, ERROR

# Top-level keys beginning x- are stripped before validation, like vars,
# which is what makes them usable as YAML anchor scratch pads.
```

## ModelDef

```yaml
models:
  name:
    provider: bedrock | openai | ollama | gemini | module.path:CustomModel
    model_id: "model-identifier"
    params: {}        # Provider-specific kwargs
```

## AgentDef

```yaml
agents:
  name:
    type: null                     # Custom factory: module.path:factory_func
    agent_kwargs: {}               # Extra kwargs for Agent() or custom factory
    model: "model_name"            # String ref to models: or inline ModelDef
    system_prompt: "..."           # System prompt string
    description: "..."             # Agent description (used in orchestration tools)
    tools: []                      # List of tool spec strings
    hooks: []                      # List of HookDef objects or import path strings
    mcp: []                        # List of MCP client names
    tool_labels: {}                # Tool name -> display label mapping
    conversation_manager: null     # ConversationManagerDef
    session_manager: null          # Per-agent SessionManagerDef (overrides global);
                                   # ~ opts this agent out of the global one
    stream: null                   # {group, title} — how this agent's output is
                                   # labelled in the event stream (Chapter 15)
    interrupt: null                # InterruptDef, or true for defaults (Chapter 6):
                                   # {tools: [], ask_user: true, ttl_seconds: null}
    history: null                  # true/false, or {enabled, group}; falls back to
                                   # the root history: default
    attachments: null              # none | reference | inline | bool | {enabled, inline}
    output_schema: null            # Import path to a Pydantic model the agent must
                                   # return (module.path:Model)
```

## AttachmentsDef

```yaml
attachments:
  default: reference | none        # Baseline for agents without their own attachments:
  tool: true                       # Expose list_references / fetch_attachment tools
  base_url: null                   # Origin for the host's own attachment routes
  authorization: null              # forwarded_props:<key> | env:<VAR> (own-origin only)
  content_url_template: null       # e.g. /attachments/{id}/content — upgrades
                                   # attachment-kind object refs to fetchable transport
```

Per-agent override on `AgentDef.attachments` (shorthands normalize to `{enabled, inline}`):

```yaml
agents:
  vision:
    attachments: inline            # manifest + inline media (ContentBlocks)
  researcher:
    attachments: reference         # manifest only (inherits if omitted)
  writer:
    attachments: none              # excluded from references
```

## RuntimeDef

```yaml
runtime:
  persist_session_state: true        # Carry pending interrupts on the AG-UI state
                                     # channel, so an approval survives a restart
                                     # (Chapter 7). Disable only when the AG-UI
                                     # endpoint is exposed straight to a browser.
  allow_invocation_overrides: false  # DEPRECATED — apply forwardedProps.agent_config
                                     # (system_prompt / model_id) per invocation.
                                     # Submit a config instead (Chapter 13).
```

## TelemetryDef

```yaml
telemetry:
  enabled: false                   # Opt in; KABOO_TELEMETRY_ENABLED overrides
  service_name: kaboo-workflows    # OTel service name
  console: false                   # Also export spans to stdout
  sample_ratio: 1.0                # 0.0-1.0
  trace_attributes: {}             # Static attributes on every span
  otlp:
    endpoint: null                 # Falls back to KABOO_OTLP_ENDPOINT / OTEL_*
    headers: null                  # Falls back to KABOO_OTLP_HEADERS / OTEL_*
```

Telemetry is initialised once per process, first call wins, so it is not
something a per-run submitted config can change.

## HookDef

```yaml
hooks:
  # Inline object form
  - type: module.path:ClassName    # or ./file.py:ClassName
    params: {}                     # Constructor kwargs

  # String shorthand (no params)
  - module.path:ClassName
```

## SessionManagerDef

```yaml
session_manager:
  provider: file | s3 | agentcore  # Built-in provider name
  type: null                        # Custom class: module.path:ClassName (overrides provider)
  params: {}                        # Constructor kwargs (session_id, storage_dir, etc.)
```

## ConversationManagerDef

```yaml
conversation_manager:
  type: strands.agent:SlidingWindowConversationManager
  params: {}                       # Constructor kwargs (window_size, etc.)
```

## MCPServerDef

```yaml
mcp_servers:
  name:
    type: ./server.py:create       # Factory function: module.path:func or ./file.py:func
    params: {}                     # Forwarded to factory (port, host, etc.)
```

## MCPClientDef

```yaml
mcp_clients:
  name:
    # Exactly one of:
    server: "server_name"          # Reference to mcp_servers entry
    url: "https://..."             # External MCP server URL
    command: ["cmd", "arg"]        # Stdio subprocess command

    transport: null                # Override: "streamable-http" | "sse" | "stdio"
    params: {}                     # Forwarded to strands MCPClient
                                   # (prefix, startup_timeout, tool_filters)
    transport_options: {}          # Transport-specific options. Declare once per
                                   # client — a second block silently replaces it.
    tool_labels: {}                # Tool name -> display label (agent labels win)
    auth: null                     # Outbound auth; "relay" shorthand expands to the
                                   # block below. Not valid with command:.
```

### MCPClientAuthDef

```yaml
    auth:
      type: relay | obo | m2m | static
      params:
        header: Authorization      # One name, or a list to send the token twice
        scheme: Bearer             # "" sends the raw value
        # relay:  token
        # static: token (required)
        # m2m:    token_url, client_id, client_secret, scope, audience, extra
        # obo:    provider (required), region, scopes, workload_name,
        #         workload_token, custom_parameters, force_authentication
```

`relay` and `obo` resolve the caller's identity from the request context, so they
only work when clients are resolved per run. See [Chapter 9](Chapter_09.md).

## DelegateOrchestrationDef

```yaml
orchestrations:
  name:
    mode: delegate
    entry_name: "agent_name"       # Agent blueprint to fork
    connections:
      - agent: "target_name"      # Agent or orchestration name
        description: "..."         # Tool description for LLM
    session_manager: null          # Override session manager
    hooks: []                      # Additional hooks
    agent_kwargs: {}               # Override agent kwargs (merged)
```

## SwarmOrchestrationDef

```yaml
orchestrations:
  name:
    mode: swarm
    agents: [agent1, agent2]       # Participating agents
    entry_name: "agent1"           # Starting agent
    chat_output: null              # Member agent whose text becomes the reply
    max_handoffs: 20               # Max handoffs
    max_iterations: 20             # Max iterations
    execution_timeout: 900.0       # Total timeout (seconds)
    node_timeout: 300.0            # Per-agent timeout (seconds)
    session_manager: null          # Swarm-level session manager
    hooks: []                      # Swarm-level hooks
```

## GraphOrchestrationDef

```yaml
orchestrations:
  name:
    mode: graph
    entry_name: "start_node"       # Node with no incoming edges
    chat_output: null              # Node whose text becomes the reply
    edges:
      - from: "node_a"
        to: "node_b"
        condition: null            # Optional: ./file.py:func or module:func
    max_node_executions: null      # Safety cap for loops
    execution_timeout: null        # Total timeout (seconds)
    node_timeout: null             # Per-node timeout (seconds)
    reset_on_revisit: false        # Reset agent state on revisit
    session_manager: null          # Graph-level session manager
    hooks: []                      # Graph-level hooks
```

## create_agui_app

```{.python notest}
create_agui_app(
    config_path,                    # Base config. With session_config_key set, the
                                    # layer every submitted config merges over.
    endpoint="/invocations",
    ping_path="/ping",
    cors_origins=None,              # Defaults to ["*"]
    cors_allow_credentials=True,
    auth=None,                      # Inbound verifier -> Principal (raise to reject)
    session_config_key=None,        # forwardedProps key carrying this run's config.
                                    # None serves config_path alone.
    allowed_mcp_hosts=None,         # Hosts a submitted mcp_client URL may point at
)
```

`session_config_key` changes the shape of the service, not just a setting: each run gets its own agents, orchestration, entry and MCP client sessions, released when its stream ends. See [Chapter 13](Chapter_13.md) for the merge rules and [Chapter 17](Chapter_17.md) for the pipeline.

---

[Next: Chapter 19 — Attachments & Multimodal →](Chapter_19.md)

**Bonus**: [Quick Recipes →](Quick_Recipes.md)
