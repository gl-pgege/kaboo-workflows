# Sub-agents in a chat UI

A multi-agent run can take minutes. Users trust it when they can see it
working — which specialist is on the job, what it is doing right now, in
words rather than function names. Every agent a delegate runs streams on the
wire as AG-UI's own sub-agent events, and kaboo-react folds them into cards.

## What the client receives

The entry agent's stream *is* the chat reply: its text and tool calls arrive as
ordinary AG-UI events, and each of its `TOOL_CALL_RESULT`s carries the same
`metadata.status` (`success` or `error`, with the `error` and any `fix` for a
failed call) as a sub-agent's below. Each agent it delegates to, at any depth,
arrives as one invocation:

- **`SUBAGENT_STARTED`** opens it, with a `subagentRunId`, the agent's `name`,
  the `parentToolCallId` of the delegating call and the
  `parentSubagentRunId` of the invocation that made that call. `metadata`
  holds its `title` and the `task` it was handed.
- **Its text, reasoning and tool calls** follow as the standard events, each
  tagged with its `subagentRunId`. Tool arguments stream as the model writes
  them, so a UI can draw a widget or a query while it is being written.
- **`TOOL_CALL_RESULT`** carries each result. Its `metadata` holds the tool's
  `label`, a `status` of `success` or `error` and, for a failed call, the
  `error` to show and the `fix` when the service suggests one; `approval` when
  the call needed one
  ([Human-in-the-loop](../workflows/human-in-the-loop.md#remembered-approvals)).
  A long JSON result stays valid JSON ([Long tool results](#long-tool-results)).
- **`SUBAGENT_FINISHED`** closes it: `completed` with its `result`, or
  `suspended` while it waits on a question or an approval. `metadata` holds its
  token `usage`, its `cost` and, for a
  [structured output](structured-outputs.md), the `outputSchema` name.
- **`SUBAGENT_ERROR`** closes it when it fails.

Each agent's messages only grow at the end: the entry agent, and each
invocation, extends only the newest message it started. Text continues text, a
tool call joins the text or calls just before it, and anything else starts a new
message. A client that appends events as they arrive therefore draws every agent
in the order it worked, whether its model reasoned before or after it spoke. A
call made after reasoning gets an assistant message of its own instead of
joining the text from before that reasoning.

A paused invocation keeps its `subagentRunId`, so the run that resumes it opens
a new segment of the same card. The events are stored and replayed like any
others, so a reload draws the same cards. `RUN_FINISHED.usage` counts every
agent, so a UI can show a true total.

## Name the cards

```yaml
agents:
  orchestrator:
    model: default
  sql_analyst:
    stream:
      title: Analyzing SQL data
  sql_visualizer:
    stream:
      group: sql_visualization
      title: Creating SQL visualizations
```

Titles are what a user reads, so write them as what the agent is *doing*,
and in the user's language — "Analyzing SQL data", not "sql_analyst". An agent
with no title gets one derived from its name, which is rarely the phrase you
would choose. Agents that share a `group` render as one card, which suits a
pair that works on the same thing in turn.

## Label the tools

```yaml
mcp_clients:
  web_research:
    url: ${GATEWAY_URL}
    transport: streamable-http
    params:
      tool_filters:
        allowed: [web-research___search, web-research___read_pages]
    tool_labels:
      web-research___search: Searching the web
      web-research___read_pages: Reading web pages
```

A sub-agent's tool call carries its label in the `metadata.label` of its
`TOOL_CALL_START` and `TOOL_CALL_RESULT`, and the card shows "Reading web pages"
instead of `web-research___read_pages`. Labels can also be set per agent, under
`agents.<name>.tool_labels`.

## Long tool results

A result longer than 16,000 characters is cut to fit. A JSON result stays valid
JSON: lists are cut to their first items and long strings end in "…", and the
object that held each cut key records in `_truncated` how many items or
characters it lost, so a table can say "+190 more":

```json
{"columns":["id","name"],"untrusted_content":{"rows":[[1,"Acme"],[2,"Globex"]],"_truncated":{"rows":190}}}
```

A cut top-level list arrives as `{"items": [...], "_truncated": {"items": N}}`.
Text that is not JSON is cut and ends in `...`.

## Test it

`TurnResult.groups` holds the cards as a client folds them, so a test can assert
what a user would see — which agents worked, nested under whom, and with which
tools ([Testing](../testing.md#multi-agent-runs)). Lab scenarios:
[delegate-dashboard](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/delegate-dashboard.yaml),
[nested-dashboard](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/nested-dashboard.yaml)
and
[parallel-dashboard](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/parallel-dashboard.yaml).
