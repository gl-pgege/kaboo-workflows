# Generative UI from any agent

CopilotKit draws UI from what an agent streams, at four levels of freedom:
your own components for your own tools, declarative surfaces from a catalog
(A2UI), one-off sandboxed visuals (Open Generative UI), and apps an MCP server
ships (MCP Apps). The first three run in CopilotKit's runtime as middleware over
the agent's stream; kaboo-workflows' part is to put every agent's tool calls on
the wire, delegates included, and to give each agent only the tiers it may use.

## Your components for your tools

Every agent's tool calls stream with their arguments as the model writes them,
sub-agents' tagged with their `subagentRunId`. A client renderer keyed by tool
name (CopilotKit's `renderToolCalls`, or a kaboo-react card) draws a widget, a
query or a dashboard while it is being written, in the chat or inside the
sub-agent's card alike. Nothing to configure here.

## Declarative surfaces (A2UI)

```yaml
agents:
  analyst:
    client_tools: [render_a2ui]
```

- **`render_a2ui` is offered to the agents allowed it.** The client sends its
  catalog as context; kaboo-workflows gives each allowed agent a `render_a2ui`
  tool described by that catalog, and the catalog reaches no other agent.
- **The surface paints from the streamed arguments.** CopilotKit's A2UI
  middleware draws it in the chat as the call streams, from a delegate as from
  the entry agent.
- **The tool doesn't wait.** It checks the components against the catalog and
  answers "Rendered surface …", or names what is wrong so the model fixes it
  and calls again.
- **An action on a surface is the person's turn.** A button that isn't handled
  in the browser reaches the agent as `forwardedProps.a2uiAction`, and the
  agent reads it as a message: `I used "break_down" on the "churn" surface (by: segment).`

Widgets in a catalog should take an id (a saved query's) rather than data, so a
surface stays small and its numbers stay live.

## One-off visuals (Open Generative UI)

```yaml
agents:
  analyst:
    client_tools: [generateSandboxedUi]
```

`generateSandboxedUi` is a browser tool: CopilotKit's Open Generative UI
middleware renders its HTML, CSS and JavaScript in a sandboxed iframe as the
arguments stream, and the browser answers the call once it has drawn it. Give
the sandbox read-only functions that fetch data by id (the lab's
`getQueryData`), so the visual carries no data of its own.

## Apps from MCP servers (MCP Apps)

A tool whose MCP server ships a UI resource reaches the run through
CopilotKit's MCP Apps middleware, which adds the server's model-visible tools
to `RunAgentInput.tools` under their own names. Allow them like any browser
tool:

```yaml
agents:
  analyst:
    client_tools: [partner_benchmarks]
```

The call waits like a browser tool. The middleware runs it against its server
when the run ends waiting on it, sends its result and the app to the chat, and
kaboo-runtime goes on with the agent in the same run, so the turn finishes with
the agent's answer. Tools the server marks as visible to the app only never
reach a model.

## Which one to use

Use your own components, or A2UI, for anything that is yours to design; Open
Generative UI for a visual no component covers; and MCP Apps for UI that comes
from a server you don't own. Say where an answer goes in the agent's prompt,
for example:

```text
Where an answer goes:
- A short fact: one or two sentences.
- A figure, a trend or a small table that answers the question now: render it
  inline with render_a2ui.
- Several parts, or anything worth keeping: a dashboard on the canvas.
- A one-off visual no catalog component covers: generateSandboxedUi.
```

Lab scenarios:
[a2ui-inline](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/a2ui-inline.yaml),
[a2ui-subagent](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/a2ui-subagent.yaml),
[a2ui-action](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/a2ui-action.yaml),
[ogui-visual](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/ogui-visual.yaml),
[mcp-partner](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/mcp-partner.yaml)
and
[viewer-parity](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/viewer-parity.yaml).
