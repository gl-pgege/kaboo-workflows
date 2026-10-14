# Upgrading

What changes between releases that you might have to act on, with the code
before and after. Each release lists who is affected; if none of it applies to
you, upgrading is a version bump.

The kaboo libraries can be upgraded in any order. Each one reads what the older
versions of the others send, and nothing an older piece sends can be mistaken
for an approval. Features that span the stack light up once every piece is
upgraded — see
[Upgrading the stack](https://gl-pgege.github.io/kaboo-docs/start-here/#upgrading-the-stack).

## 0.28

kaboo-workflows moves to the AG-UI 1.0 generation of `ag-ui-protocol` and
`ag-ui-strands`, and to `mcp` 2.

### Dependencies

**Who:** apps that pin any of these packages themselves.

```diff
- "strands-agents>=1.44.0,<2.0.0",
- "mcp>=1.24.0,<2",
- "ag-ui-strands>=0.2.0,<0.3",
- "ag-ui-protocol>=0.1.19,<1",
+ "strands-agents>=1.55.0,<2.0.0",
+ "mcp>=2.0.0,<3",
+ "httpx2>=2.5.0,<3",
+ "ag-ui-strands>=1.0.1,<2",
+ "ag-ui-protocol>=1.0.0,<2",
```

Tested with strands-agents 1.61, mcp 2.1, ag-ui-strands 1.0.1 and
ag-ui-protocol 1.0.0. Approvals and questions resume exactly as before:
kaboo-workflows answers them itself rather than through ag-ui-strands' own
interrupt protocol.

Clients need no change: kaboo-runtime 0.6 and kaboo-react 0.10 work with 0.28,
and kaboo-runtime 0.7 and kaboo-react 0.11 work with 0.27. Runs now also carry
ag-ui-strands' `RAW` events, one per model call with its usage and latency.

### AG-UI activity deltas carry typed patch operations

**Who:** code that reads `ag_ui.core` events in Python, such as tests that
inspect the activity stream.

`ag-ui-protocol` 1.0 types `ActivityDeltaEvent.patch` as JSON Patch operation
models instead of dicts. What goes over the wire is unchanged; read the patch as
a client receives it:

```diff
- ops = event.patch
+ ops = event.model_dump(mode="json", by_alias=True, exclude_none=True)["patch"]
```

### Custom MCP servers get the SDK's `MCPServer`

**Who:** anyone who subclasses `kaboo_workflows.mcp.MCPServer` or sets
`server_params`.

mcp 2 renamed `FastMCP` to `MCPServer` in `mcp.server.mcpserver`, and
`_register_tools` receives that class. Its `tool()` decorator is unchanged.

```diff
- from mcp.server.fastmcp import FastMCP
+ from mcp.server.mcpserver import MCPServer as SdkMCPServer
  from kaboo_workflows.mcp import MCPServer

  class CalculatorServer(MCPServer):
-     def _register_tools(self, mcp: FastMCP) -> None:
+     def _register_tools(self, mcp: SdkMCPServer) -> None:
          @mcp.tool()
          def add(a: float, b: float) -> float: ...
```

`server_params` still reach the server's constructor, which no longer takes
`host`, `port`, `stateless_http` or `json_response`. kaboo-workflows passes the
host and port from the server's definition, and serves `streamable-http`
stateless with JSON responses as before.

### MCP clients take `httpx2`

**Who:** anyone who passes `transport_options.http_client` or
`transport_options.auth` in code, or writes their own auth for an MCP client.
YAML `auth:` settings need no change.

mcp 2 makes its HTTP requests with `httpx2`, so a client or auth you pass must
come from it. kaboo-workflows' `RelayTokenAuth`, `StaticTokenAuth` and
`M2MClientCredentialsAuth` are `httpx2.Auth` strategies now.

```diff
- import httpx
+ import httpx2

- transport_options={"http_client": httpx.AsyncClient(auth=RelayTokenAuth())}
+ transport_options={"http_client": httpx2.AsyncClient(auth=RelayTokenAuth())}
```

Other code of yours that uses the `mcp` package directly follows mcp's own 2.0
migration.

### Bedrock prompt caching settings

**Who:** Bedrock models that set `cache_prompt` or `cache_tools`.

strands deprecates both in favor of `cache_config` fields. They still work, with
a warning:

```diff
  params:
-   cache_prompt: default
-   cache_tools: default
-   cache_config: {strategy: auto}
+   cache_config: {strategy: auto, system_prompt_ttl: true, tools_ttl: true}
```

### Question `input_type` is removed in 0.29

The deprecated `input_type` and question `type` (see 0.27) still work in 0.28,
and are removed in 0.29.

## 0.27.1

**Who:** anyone who installed 0.27.0 fresh, or pins one of these packages
themselves.

0.27.0 allowed dependency versions it was never tested with, and a fresh
install picked up `ag-ui-strands` 1.0, whose own interrupt tracking refuses a
resumed run (`PENDING_INTERRUPTS`), so every approval and question failed on
resume. The ranges now stop at the versions kaboo-workflows is tested with:

```diff
- "ag-ui-strands>=0.2.0",
- "ag-ui-protocol>=0.1.19",
- "mcp>=1.24.0",
- "langfuse>=3.0.0",
+ "ag-ui-strands>=0.2.0,<0.3",
+ "ag-ui-protocol>=0.1.19,<1",
+ "mcp>=1.24.0,<2",
+ "langfuse>=3.0.0,<5",
```

`strands-agents` keeps `<2.0.0` and is tested up to 1.61. If your app pins a
newer version of one of these, pip now reports the conflict instead of
installing a combination that doesn't work.

## 0.27

Questions are multiple choice only, tool approvals gain opt-in remembered
decisions, and every answer the person can give reaches the model as approved,
denied with a reason, or skipped.

### `ask_user` asks multiple-choice questions only

**Who:** every agent with `interrupt: true` or `interrupt.ask_user`, and any
prompt that teaches the model `input_type` or free-text questions.

Every question now offers two or more options, `multiple` makes it
multi-select, and the person can always pick **Other** and answer in their own
words. The agent never lists "Other" itself; the tool strips it. A call
without enough options gets a tool error that teaches the rule, so the model
asks again with options instead of reaching the person.

```diff
- ask_user(question="What should I focus on?")
- ask_user(question="Which sectors?", input_type="checkbox", options=["B2B", "B2C"])
+ ask_user(question="What should I focus on?", options=["Revenue", "Churn", "Costs"])
+ ask_user(question="Which sectors?", options=["B2B", "B2C"], multiple=True)
```

Update prompts the same way:

```diff
  system_prompt: |
-   Use input_type "radio" when one option applies and "checkbox" when several do.
+   Ask with ask_user, offering options; set multiple when several can apply.
```

**Deprecated, removed in 0.29:** `input_type` and a question's `type` of
`radio` or `checkbox` still work, as single and multi-select, and log a
deprecation warning. `text` is refused. Question requests also still carry
`type` for kaboo-react before 0.10.

The answer reaches the model as before — a `{question: answer}` map — with a
list for multi-select and the person's own words for Other. A skipped question
returns "The person skipped this question."

### Custom requests are their own kind, not text questions

**Who:** tools that raise a `form` interrupt with a text question and a
discriminator such as `kind` or `kaboo_kind`, to make a UI show a file or data
source picker.

Raise your own request kind instead. kaboo-react renders it with the renderer
you register for that `type`, and shows a minimal card with Skip when nothing is
registered.

```diff
  response = tool_context.interrupt(
      name="request_datasources",
      reason={
-         "type": "form",
-         "kind": "datasources",
-         "kinds": kinds,
-         "questions": [{"question": reason, "type": "text"}],
+         "type": "datasources",
+         "message": reason,
+         "kinds": kinds,
      },
  )
```

A skipped request answers `{"status": "cancelled", "skipped": True}`, so a tool
that already treats `"cancelled"` as "no answer" needs no change. See
[Ask the user](recipes/asking-the-user.md).

### Only an approval runs a gated call

**Who:** clients and custom approval cards that resume tool gates.

A gated call runs only when the answer's `status` is `"approved"`. Before, any
answer other than `"cancelled"` ran it.

```diff
  resume = [{"interruptId": gate_id, "status": "resolved", "payload": {"status": "approved"}}]
- resume = [{"interruptId": gate_id, "status": "resolved", "payload": {"status": "rejected"}}]
+ resume = [{"interruptId": gate_id, "status": "resolved", "payload": {"status": "cancelled", "reason": "Use the final title"}}]
```

- **Deny** with `{"status": "cancelled", "reason"?}`. The model reads
  "Declined: <reason>".
- **Skip** with a plain AG-UI `cancelled` entry, or by sending a message. The
  model reads "Skipped: …".
- **Approve** with `{"status": "approved", "scope"?, "tool_input"?}`.

The model's wording changed: "User rejected this action." is now "Declined.",
and a call closed by a new message reads "Skipped: the person sent a message
instead of answering."

### A message sent while something waits skips it

**Who:** hosts and clients; no config change.

A run that cancels every open request and ends with a new user message — the
AG-UI form of "the person moved on" — is a new turn: the waiting calls close as
skipped, their activity cards close, and the agent answers the message. A plain
new message (no `resume`) did this already. kaboo-react 0.10 sends it for you.

### The deadline moved into `metadata`, and the agent enforces it

**Who:** agents with `interrupt.ttl_seconds`, and clients that read the AG-UI
interrupt's `expiresAt`.

The AG-UI client refuses to resume a thread, or to send anything on it, once an
open interrupt's own `expiresAt` has passed — the chat was stuck. The deadline
now travels only in `metadata.expiresAt`, and an approval answered after it does
not run.

```diff
- const deadline = interrupt.expiresAt
+ const deadline = interrupt.metadata?.expiresAt
```

Without `ttl_seconds`, approvals wait indefinitely, like questions.

### Remembered approvals (opt-in)

**Who:** anyone who wants "Allow for this chat" or "Always allow".

```yaml
interrupt:
  tools:
    - "merchant___*"
    - { name: sql___run_query, remember: once }
  skip_read_only: true
  remember: thread
```

- `tools` takes names, globs, or `{name, remember, subject}` objects.
- `skip_read_only` lets tools annotated `readOnlyHint: true` run without asking.
- `remember` is the widest scope a person may choose: `once` (the default),
  `thread` or `always`.
- `subject` is an import spec deciding what a remembered decision covers.

Wider scopes are offered only when the host remembers decisions: it sends
`forwardedProps.kaboo_grants` and the run publishes its open requests in
`kaboo_session.approvals`. kaboo-runtime 0.6 does both when given a grant store.
See [Human-in-the-loop](workflows/human-in-the-loop.md#remembered-approvals).

### Tools can ask for approval themselves (opt-in)

**Who:** anyone whose tools are generic (one tool that does many things, such
as a GraphQL `run_mutation`) and whose service already knows what each call
needs.

```yaml
interrupt:
  tool_approvals: true
```

A tool that won't run a call yet returns a `kaboo_approval` reply instead of a
result. The run pauses with an approval request carrying the tool's `request`
ID, and the tool is called once more if the person approves. The host must
tell the tool's service about the decision before the run continues
(kaboo-runtime 0.6's `onDecision`). Nothing changes until you turn it on. See
[Tools that enforce their own approvals](workflows/human-in-the-loop.md#tools-that-enforce-their-own-approvals),
including the checklist for the service.

### For hosts

- Open approval requests are published in `state.kaboo_session.approvals`,
  from both the single-agent and the Swarm/Graph adapters: `interruptId`,
  `toolCallId`, `tool`, `subject`, `scopes`, `input`, `expiresAt`, and
  `request` when a tool asked for the approval. Forward a decision with a
  `request` to that tool's service, and don't store it as a grant.
- The tool's activity entry records how a gated call was decided
  (`approval: {decision, by, scope?, reason?}`).
- The private `_build_resume_responses` helpers are replaced by one shared
  builder in `kaboo_workflows.adapters._interrupts`.
