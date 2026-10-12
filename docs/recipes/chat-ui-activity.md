# Activity a chat UI can show

A multi-agent run can take minutes. Users trust it when they can see it
working — which specialist is on the job, what it is doing right now, in
words rather than function names. Two pieces of config produce that, and
kaboo-react renders it.

## What the client receives

`create_agui_app` folds every agent's stream into **activity groups** and sends
them alongside the chat reply: a snapshot first, then JSON Patches as they
change (`activity_deltas`). Each group is one card in the UI:

- **the agent's title**, and the task it was handed;
- **its tool calls**, each with a readable label, arguments and result;
- **its text**, its token usage, and any [structured output](structured-outputs.md);
- **its children**, for agents it delegated to.

The entry agent has no card: its stream *is* the chat reply.

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

A tool call shows its label (`toolLabel`) instead of
`web-research___read_pages`. Label every tool a user will see; an unlabelled
one shows its raw name.

## Keep it fast

- **Leave `activity_deltas` on.** Sending a full tree on every change grows
  with the run; on long multi-agent runs that is what fills a response
  ([Serve on AgentCore in production](production-serving.md#stay-under-the-response-cap)).
- **Do not route the chat reply through a group.** The entry agent's text is
  the answer and streams as ordinary AG-UI text messages.

## Test it

`TurnResult.groups` holds the cards as the UI would, so a test can assert the
tree a user would see — which agents worked, nested under whom
([Testing](../testing.md#multi-agent-runs-and-activity)).
