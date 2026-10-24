# Recipes

The configuration chapters say what every field does. These recipes show how
the pieces fit together in a real service — each one is a pattern running in
production in [Kaboo](https://gl-pgege.github.io/kaboo-docs/), written so you
can lift it into your own project.

| Recipe | Reach for it when |
|--------|-------------------|
| [Serve on AgentCore in production](production-serving.md) | You deploy behind AWS AgentCore Runtime, or any host with response caps and idle reaping |
| [Compose a workflow per run](per-run-workflows.md) | One service runs many workflows — per tenant, per project, per admin-authored row |
| [Skills](skills.md) | Agents need reusable instructions, loaded only when a task calls for them |
| [Call MCP tools as the signed-in user](user-identity-mcp.md) | Tools behind a gateway must act with the caller's permissions |
| [Tools that need run context](tools-with-run-context.md) | A tool keeps state per conversation or must act once per turn |
| [Guard hooks for every agent](guard-hooks.md) | Runs should survive malformed transcripts, retry loops and expired sign-ins |
| [Ask the user, and request inputs](asking-the-user.md) | An agent needs an answer, a file or a data source mid-run |
| [Shared state](shared-state.md) | The person and every agent should see what the conversation built, without looking it up |
| [Generative UI from any agent](generative-ui.md) | Answers should come as surfaces, visuals or partner apps in the chat |
| [Structured outputs a UI can render](structured-outputs.md) | An agent's result should arrive as typed data for a card, not prose |
| [Images and file attachments](attachments-and-images.md) | Users attach files, or tools return images |
| [Sub-agents in a chat UI](chat-ui-activity.md) | Users should see which agent is working on what, in words |
| [Cost and context control](cost-and-context.md) | Long, tool-heavy runs cost too much or outgrow the context window |

[Testing your workflows](../testing.md) covers proving any of these with a
scripted model, and [Best practices](../best-practices.md) collects the rules
of thumb behind them.
