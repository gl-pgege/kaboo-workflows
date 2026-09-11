# Chapter 9: MCP — External Tool Servers

[← Back to Table of Contents](README.md) | [← Previous: Conversation Managers](Chapter_08.md)

---

The Model Context Protocol (MCP) lets agents connect to external tool servers. kaboo-workflows supports three connection modes and manages the full server lifecycle.

## Architecture

```
mcp_servers:  → Define managed local servers (kaboo-workflows starts/stops them)
mcp_clients:  → Define connections to servers (local, remote, or subprocess)
agents:
  my_agent:
    mcp: [client_name]  → Attach MCP clients as tool providers
```

## Mode 1: Managed Local Server

You define a server, kaboo-workflows starts it in a background thread before creating agents, and stops it on shutdown:

```yaml
mcp_servers:
  calculator:
    type: ./server.py:create
    params:
      port: 9001

mcp_clients:
  calc:
    server: calculator                # References the server above
    params:
      prefix: calc                    # Tools become calc_add, calc_multiply, etc.

agents:
  assistant:
    mcp: [calc]
    system_prompt: "Use calc tools for math."

entry: assistant
```

The `type` field points to a factory function that returns an `MCPServer` instance:

```{.python notest}
# server.py
from mcp.server.fastmcp import FastMCP
from kaboo_workflows.mcp import MCPServer

class CalculatorServer(MCPServer):
    def _register_tools(self, mcp: FastMCP) -> None:
        @mcp.tool()
        def add(a: float, b: float) -> float:
            """Add two numbers."""
            return a + b

        @mcp.tool()
        def multiply(a: float, b: float) -> float:
            """Multiply two numbers."""
            return a * b

def create(name: str, port: int = 9001) -> CalculatorServer:
    return CalculatorServer(name=name, port=port)
```

The factory receives `name` (from the YAML key) plus everything in `params`.

## Mode 2: Remote URL

Connect to an existing MCP server over HTTP — no server management needed:

```yaml
mcp_clients:
  aws_docs:
    url: https://knowledge-mcp.global.api.aws
    transport: streamable-http
    params:
      prefix: aws
      startup_timeout: 30
```

## Mode 3: Stdio Subprocess

Spawn a local process that speaks MCP over stdin/stdout:

```yaml
mcp_clients:
  filesystem:
    command: ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    params:
      prefix: fs
```

## The `transport` Field

Transport auto-detection usually works, but you can override it:

| Transport | When to Use |
|-----------|-------------|
| `streamable-http` | Default for URLs and managed servers. Modern MCP transport. |
| `sse` | Older Server-Sent Events transport. Auto-detected if URL ends in `/sse`. |
| `stdio` | Set automatically for `command:` mode. Not valid for managed servers. |

## Client `params`

The `params` dict on an MCP client is forwarded to strands' `MCPClient` constructor:

| Param | Type | What It Does |
|-------|------|-------------|
| `prefix` | string | Prefix all tool names from this server (e.g., `calc_add`) |
| `startup_timeout` | number | Seconds to wait for the server to respond |
| `tool_filters` | mapping | Restrict which of the server's tools the agent sees |

`tool_filters` is forwarded to strands rather than implemented here, so its
matching rules are strands'. In practice that means **exact names, not globs**:

```yaml
mcp_clients:
  platform_read:
    url: ${GATEWAY_URL}
    params:
      tool_filters:
        allowed:
          - platform___get_record
          - platform___list_records
```

Filtering one client down to a read-only subset, and pointing a second client at
the same URL with the full set, is how one server becomes two capability levels.
Give each to a different agent — an agent holding both registers the overlapping
tools twice under one name.

## Client `transport_options`

Transport-specific options forwarded to the transport factory:

```yaml
mcp_clients:
  authenticated_server:
    url: https://internal.example.com/mcp
    transport_options:
      headers:
        Authorization: "Bearer ${API_TOKEN}"
```

Available options vary by transport:

- **stdio**: `env`, `cwd`, `encoding`, `encoding_error_handler`
- **sse**: `headers`, `timeout` (default 5), `sse_read_timeout` (default 300), `auth`, `httpx_client_factory`
- **streamable-http**: `headers`, `http_client`, `terminate_on_close`, `timeout`

### Timeouts on `streamable-http`

`timeout` takes either a number of seconds applied to every phase, or a dict
naming httpx phases individually. **Phases you do not name keep httpx's 5s
default**, which is the trap: a tool that streams for minutes needs `read`
raised explicitly.

```yaml
mcp_clients:
  slow_queries:
    url: ${GATEWAY_URL}
    transport: streamable-http
    transport_options:
      terminate_on_close: false
      timeout: { read: 840 }        # connect/write/pool stay at 5s
```

Declare `transport_options` **once per client**. YAML mappings are last-wins, so
a second `transport_options:` further down the same client silently replaces the
first — including a carefully raised `read` timeout — with no warning from the
parser.

When you pass your own `http_client`, both `headers` and `timeout` are ignored;
configure them on the client you supply.

## Outbound Auth — the `auth:` Field

`transport_options.headers` is a fixed dict, decided when the config is parsed.
That is enough for a static API key and useless for anything that has to be
resolved per call — a caller's own token, or one that expires. `auth:` is the
declarative form for those.

```yaml
mcp_clients:
  platform:
    url: ${GATEWAY_URL}
    transport: streamable-http
    auth:
      type: relay
      params:
        header: Authorization
```

`auth: relay` is shorthand for the same strategy with no params, which lands in
the same place because `Authorization` is the default header. `auth:` is not
valid alongside `command:` — a stdio subprocess has no HTTP request to attach a
header to.

### The three strategies

| `type` | Whose identity | Where the token comes from | Safe on a shared client |
|--------|----------------|----------------------------|-------------------------|
| `relay` | The inbound caller | The current request's principal, unchanged | **No** — see below |
| `m2m` | The workflow service itself | OAuth2 client-credentials grant, cached | Yes |
| `static` | Whoever the token belongs to | A fixed value you supply | Yes |

`relay` resolves identity from the ambient request context, which strands
snapshots when a client **starts**. A client started at boot has no caller to
relay, so it only works when clients are resolved per run — that
is, when the app is serving runs that submit their own config
(`session_config_key=`, [Chapter 17](Chapter_17.md)). `m2m` and `static` have no
such constraint because they do not depend on who is calling.

There is deliberately no on-behalf-of strategy. Token exchange belongs to
whatever sits between the agent and the service — a gateway can exchange
against an authorization server that knows what the downstream audience should
be, where this library would have to be told, per deployment, which of several
audiences each tool call needed. An `obo` strategy existed through 0.19.0 and
was removed in 0.20.0; `build_auth("obo", ...)` now raises.

### Common params

Every strategy accepts these:

| Param | Default | Purpose |
|-------|---------|---------|
| `header` | `Authorization` | Header name, or a **list** of names |
| `scheme` | `Bearer` | Prefix before the token; `""` sends the raw value |

`header` accepting a list (0.19.0) exists for gateways that consume the header
they authenticate on. A managed gateway that validates the caller on
`Authorization` and then replaces it with its own outbound credential leaves the
target seeing nothing — so the token has to be sent twice, under two names:

```yaml
    auth:
      type: relay
      params:
        header:
          - Authorization                 # the gateway authenticates on this
          - x-kaboo-run-token             # the target reads this one
```

The same value is written to every name listed. An empty list is rejected.

### Per-strategy params

**`relay`** — forward the caller's token untouched.

| Param | Default | Purpose |
|-------|---------|---------|
| `token` | principal's token | Explicit override, for a client bound to a captured token |

**`static`** — a fixed token on every request.

| Param | Default | Purpose |
|-------|---------|---------|
| `token` | required | The value to send |

**`m2m`** — the service's own machine identity, cached until shortly before expiry.

| Param | Default | Purpose |
|-------|---------|---------|
| `token_url` | required | OAuth2 token endpoint |
| `client_id` / `client_secret` | required | Client credentials |
| `scope` | none | Space-delimited scopes |
| `audience` | none | Audience parameter (e.g. Auth0) |
| `extra` | `{}` | Extra form fields on the token request |

### How it reaches the wire

For `streamable-http`, the strategy is attached to a dedicated `httpx.AsyncClient`
built for that MCP client; for `sse` it becomes `transport_options.auth`. Either
way the token is resolved **per request**, not once at parse time. If the
strategy resolves to no token — `relay` with no principal in context, most often
— the header is simply omitted and the request goes out unauthenticated, so a
misconfigured relay looks like a 401 from the target rather than an error here.

## Lifecycle Management

kaboo-workflows handles the startup ordering automatically:

1. Start all MCP **servers** (in parallel)
2. Wait for all servers to be **ready** (TCP port check with configurable timeout)
3. Create agents (which auto-start MCP **clients**)

On shutdown (via context manager or `.stop()`):

1. Stop all **clients** first
2. Then stop all **servers**

Always use the MCP lifecycle context manager:

```{.python notest}
resolved = load("config.yaml")

with resolved.mcp_lifecycle:
    result = resolved.entry("Hello!")
```

Or for async contexts:

```{.python notest}
async with resolved.mcp_lifecycle:
    result = await resolved.entry.invoke_async("Hello!")
```

### How Long a Client Session Lives

A **server** is a process, so it belongs to the process that started it. A **client** is a session, and its lifetime is a choice:

- Serving one fixed config (`create_agui_app("config.yaml")`), clients are opened once and held for the process, so every run reuses them.
- Serving runs that submit their own config (`session_config_key=`, see [Chapter 17](Chapter_17.md)), each run resolves its own clients and they are closed when its stream ends.

Per-run clients cost a handshake per client per turn, which is small beside a model call, and they buy two things worth more than that. `MCPClientError: the client session is not running` stops being possible rather than being retried, because a session that cannot outlive its run cannot be found dead at the start of the next one. And `relay` auth becomes reachable, because strands captures the ambient identity when a client *starts* — a client started at boot has no caller to relay.

Servers are still shared either way, so a `server:` client declared by a run binds to the process's already-running server.

## MCPClientDef Validation

Exactly **one** of `server`, `url`, or `command` must be set on each client. Setting zero or more than one raises a validation error:

```
MCPClientDef requires exactly one of 'server', 'url', or 'command'; got none.
```

## Combining Multiple MCP Sources

A single agent can use tools from multiple MCP clients:

```yaml
agents:
  super_agent:
    mcp:
      - calc_client
      - aws_knowledge
      - filesystem
    system_prompt: "You have math, AWS docs, and filesystem access."
```

> **Tips & Tricks**
>
> - The `prefix` parameter is your friend. It namespaces tools to avoid collisions: `calc_add` vs `aws_add`.
> - For development, managed servers (Mode 1) are the most convenient — everything starts and stops with your script.
> - For production, prefer remote URLs (Mode 2) — deploy MCP servers independently and connect agents to them.
> - Server transport defaults to `streamable-http`. You can also use `sse` for older MCP servers.
> - MCP servers support `server_params` which are forwarded to FastMCP constructor — useful for `stateless_http`, `json_response`, etc.

---

[Next: Chapter 10 — Orchestrations →](Chapter_10.md)
