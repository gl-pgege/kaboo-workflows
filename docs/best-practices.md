# Best practices

Rules of thumb from running kaboo-workflows in production, each with the reason
behind it. The [recipes](recipes/index.md) show them in full.

## Configuration

- **Keep models and MCP clients apart from workflows, and refer to them by
  name.** The same workflow then runs against real models in production and
  scripted ones in tests, and a workflow stored in a database never carries a
  URL or a credential ([Testing](testing.md#testing-your-own-tools),
  [per-run workflows](recipes/per-run-workflows.md)).
- **Secrets live in the base config; escape `${` in everything else.** A
  submitted workflow is interpolated against the server's environment, so a
  user-authored `${…}` would resolve there too. `$${` is a literal `${`.
- **Grant tools by exact name, a few at a time.** `tool_filters.allowed` is an
  exact-match list. Narrow clients per job and a ceiling of about 20 tools per
  agent keep prompts small and tool choices good.
- **Validate on save, not on first run.** `load_session_config` checks a
  workflow exactly as a run would and names what is missing; add your own
  rules after it and refuse any unfilled placeholder.
- **Configure only what the model supports.** Adaptive thinking, prompt caching
  and media blocks are model features; a config written for one model can fail
  at the provider on another.
- **Use module paths in workflows submitted per run.** Relative paths resolve
  from a config file's directory; a submitted workflow has none, so they
  resolve from the service's working directory.

## Prompts and skills

- **Short prompts, reusable skills.** Inline only the rules that must always
  hold; list the rest so agents load them when a task calls for it
  ([Skills](recipes/skills.md)).
- **Tell agents to reload what compression removed.** Long runs compress old
  messages, and an agent will happily carry on from a skill it can no longer
  see.
- **Keep the start of a prompt stable.** Prompt caching keys on the prefix;
  append changing lines, such as today's date, at the end.
- **Write titles, labels and schema descriptions for their reader.** Stream
  titles and tool labels are read by users; schema field descriptions are read
  by the model. Neither should look like an identifier.

## Reliability

- **Put guard hooks on every agent.** Repair transcripts before each model
  call, refuse a call that has failed twice, stop cleanly when a sign-in
  expires, and cap tool calls ([Guard hooks](recipes/guard-hooks.md)).
- **Respect Bedrock's conversation rules.** Every tool call answered in the next
  message, no two messages in a row from the same role, no empty text blocks.
  A violation stops the run without an error; `ScriptedModel` enforces the
  pairing rule so tests catch it first.
- **One voice asks the user.** Grant `ask_user` to the agent that talks to the
  user, ask once with every open question, and treat a cancelled request as an
  answer ([Ask the user](recipes/asking-the-user.md)).
- **On capped hosts, set a response budget.** A long turn then pauses and
  continues in a fresh response instead of being cut off; on AgentCore also
  report busy while a run is open
  ([Serve on AgentCore](recipes/production-serving.md)).

## State and identity

- **Conversation state belongs to the client.** History and pending interrupts
  travel on the AG-UI state channel, so any replica can serve any turn and a
  restart loses nothing. Do not keep conversation state in process memory.
- **Bound anything process-local, and key it deliberately.** Thread id for the
  conversation, run id for the request, turn id for a side effect that must
  happen once across an interrupt and its resume
  ([Tools that need run context](recipes/tools-with-run-context.md)).
- **Identity comes only from the verified principal.** Forwarded props are
  client input; validate them like any other.
- **Relay a user's token only on clients built per run.** Per-run clients are
  what keep one user's token out of another's run
  ([Call MCP tools as the signed-in user](recipes/user-identity-mcp.md)).

## Cost

- **Cache the system prompt, the tool list and the conversation.** Three of
  Bedrock's four cache points pay for themselves on the first tool loop.
- **Think where it pays.** Thinking on the agent that plans and judges, off for
  the ones executing clear instructions.
- **Do not resend what the agent no longer needs.** Clear old tool results
  within a run and mask them in history between runs
  ([Cost and context control](recipes/cost-and-context.md)).

## Testing

- **Script a test for every workflow you ship.** A scripted run exercises the
  real tools, hooks, prompts and orchestration with no model, network or
  server ([Testing your workflows](testing.md)).
- **Test what runs, not what is stored.** For composed workflows, feed the
  composer's output to the pipeline and assert on the prompt the model
  received.
- **Prove restarts on a fresh pipeline.** Resuming on a new `Pipeline` with the
  persisted state is the only test that a pause survives a deploy.
- **Keep model evaluations few and deliberate.** Scripted tests prove
  behaviour; [evaluations](evaluation.md) prove answer quality, at the price of
  a real model call per item.

## Observability

- **Turn on telemetry** and send traces to an OTLP backend such as Langfuse
  ([Evaluation](evaluation.md#enable-telemetry)).
- **Log every model call's latency and retries with its run id.** It answers
  "why was that slow?" when tracing is off.
- **Watch token usage per run** after every prompt or config change; caching in
  particular breaks silently.
