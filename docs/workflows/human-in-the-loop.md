# Human-in-the-loop

An agent can pause a run for the person in two ways:

- It **asks for something** with a tool the browser runs: a question
  (`ask_user`), a file, a data source. The run ends waiting on the call, the
  browser answers it, and the agent that asked reads the answer.
- A **gated tool waits for approval** before it runs. The run ends on an
  interrupt, and the person's decision resumes it.

Either way the run resumes minutes or days later, on any replica, and the agent
carries on mid-thought.

## Asking the person

Tools the browser runs are declared by the client with each run, in
`RunAgentInput.tools`. An agent may call the ones its `client_tools` list
names, delegates included:

```yaml
agents:
  coordinator:
    client_tools: [ask_user]
    system_prompt: |
      Ask the person (ask_user) only when the question is ambiguous in a way
      that changes the answer.
  analyst:
    client_tools: [ask_user, request_datasources]

orchestrations:
  team:
    mode: delegate
    entry_name: coordinator
    connections:
      - { agent: analyst, description: "Answers data questions" }

entry: team
```

- **The call waits.** The run ends with `RUN_FINISHED` whose outcome lists the
  call in `pendingToolCallIds`. Nothing answers it on the server, and no result
  is made up.
- **The answer goes to the agent that asked.** The browser sends the next run
  with a tool message for the call. When a delegate asked, the delegate resumes
  with the answer, and its coordinator is told in plain words what was asked
  and answered, so it doesn't ask again.
- **A delegate's call is tagged** with the delegate's `subagentRunId`, so the
  chat shows it in the main transcript, labelled with the agent.
- **An agent never sees a tool it isn't allowed.** Tools the browser declares
  but no `client_tools` list names reach no agent, and context the client sends
  for a tool reaches only the agents allowed that tool.

Full runnable project: [`examples/18_hitl`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/18_hitl).

The client owns the tool: its name, its schema, and what the person sees.
kaboo-react's `useAskUserTool` asks multiple-choice questions, with Other for
an answer in the person's own words, and answers a call whose arguments don't
match the schema with an error the model corrects; `useRequestTool` draws any
other request as a panel. See [Ask the user](../recipes/asking-the-user.md).

Lab scenarios:
[question-orchestrator](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/question-orchestrator.yaml),
[question-subagent](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/question-subagent.yaml),
[question-parallel](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/question-parallel.yaml),
[question-malformed](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/question-malformed.yaml)
and
[request-datasources](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/request-datasources.yaml).

## Approving tool calls

`interrupt.tools` lists the tools that wait for the person's approval *before
they run*:

```yaml
agents:
  merchant:
    interrupt:
      tools:
        - "merchant___*"                          # a glob
        - { name: delete_store, remember: once }  # a per-tool override
      skip_read_only: true
      remember: thread
      ttl_seconds: 3600
```

- **`tools`** — names, `fnmatch` globs, or `{name, remember, subject}` objects.
  The first match applies.
- **`skip_read_only`** — tools annotated `readOnlyHint: true` run without
  asking. Tools without annotations follow MCP's defaults and count as not
  read-only. MCP servers declare annotations; localcore-gateway and AgentCore
  Gateway relay them for MCP server targets.
- **`remember`** — the widest decision a person may make: `once` (the default),
  `thread` ("Allow for this chat") or `always` ("Always allow").
- **`subject`** — an import spec, `(tool_name, tool_input) -> {key, label} |
  str | None`, deciding what a remembered decision covers. By default it is the
  whole tool; return `None` when a call needs no approval. Include the resource
  the call acts on (an account, a store, a recipient's domain), so a remembered
  decision never reaches further than what the person saw: `store:summit/catalog`
  rather than the tool name.
- **`ttl_seconds`** — an optional deadline. It travels in the request's
  `metadata.expiresAt` (never AG-UI's own `expiresAt`, which makes the client
  refuse the thread) and the agent enforces it: an approval answered after the
  deadline does not run the call. Without it, approvals wait with no limit.

A gated call pauses the run with an interrupt on `RUN_FINISHED`, published by
ag-ui-strands and filled in by kaboo: `reason: "kaboo:approval"`, the
`toolCallId` of the call it is about (a delegate's own call, not the one that
delegated to it), the delegate's `subagentRunId`, a `message`, and a
`responseSchema` for the answer. Its `metadata` carries `tool_name`,
`tool_input`, `subject` (`key`, `label`), `scopes` (what the person may choose),
with a deadline `expiresAt`, and `request` when a tool asked for the approval
itself ([below](#tools-that-enforce-their-own-approvals)).

## Answers

The client resumes by answering each open interrupt by its `id`:

| The person… | Resume entry | The agent |
|---|---|---|
| approves | `{"status": "resolved", "payload": {"status": "approved", "scope"?: "once" \| "thread" \| "always", "tool_input"?: {...}}}` | runs the call, with the person's edit if given |
| denies | `{"status": "resolved", "payload": {"status": "cancelled", "reason"?: "..."}}` | cancels it; the model reads "Declined: <reason>" |
| skips | `{"status": "cancelled"}` | cancels it; the model reads "Skipped: …" |

Only an `approved` answer runs a gated call; anything else does not. A request
the resume leaves out counts as skipped, never as approved. A denial is sent as
`cancelled` so an agent that predates denial reasons cannot mistake it for an
approval. The tool body runs from the top on resume, with the person's edits.

## Remembered approvals

"Allow for this chat" and "Always allow" are opt-in on both sides:

- **The agent** offers them when its `remember` allows, and only when the host
  remembers decisions: the host sends `forwardedProps.kaboo_grants`, and the run
  publishes its open requests in `kaboo_sessions.approvals` (on by default; see
  `runtime.persist_session_state`).
- **The host** saves the person's decision and sends the permissions on every
  run as `kaboo_grants: [{tool, subject, scope}]`. A call matching one runs
  without asking. kaboo-runtime does this when given a grant store.

The agent never stores a decision itself. The person's own answer on a request
wins over a permission that answer created, so an edit still applies. The
call's `TOOL_CALL_RESULT` records how it was decided, in
`metadata.approval: {decision, by: "person" | "permission", scope?, reason?}`.

## Tools that enforce their own approvals

Some tools can't be judged by name. A generic tool such as `run_mutation` does
a different thing on every call, and only its service knows which account,
store, kind of change or item a call touches. Such a service decides, asks and
remembers, and the agent only pauses for it. Whoever enforces approvals also
remembers them.

```yaml
agents:
  merchant:
    interrupt:
      tool_approvals: true   # tools may ask the person to approve a call
```

When a call isn't covered, the tool runs nothing and returns a normal (not
error) result whose structured content, or JSON text, is a `kaboo_approval`
reply:

```json
{
  "kaboo_approval": {
    "request": "apr_7Qm2",
    "subject": {"key": "store:summit/catalog", "label": "catalog changes in Summit Outfitters"},
    "scopes": ["once", "thread"],
    "message": "Update the price of 3 variants"
  }
}
```

Return it as an object (structured content). It must not be an error result:
gateways such as localcore-gateway reduce an error to its text and drop its
structured content. A tool that returns the reply as a JSON string still works:
the MCP Python SDK's server wraps a string result in `{"result": "..."}`, and
that is read too.

- **The run pauses** with the same approval request a gated call raises, plus
  `request`. The reply never reaches the model; the call shows as waiting.
- **The person approves:** the tool is called once more, with their edits if
  any, and its result goes to the model. If the tool asks again, the model
  reads "Still needs approval", so the agent can't loop.
- **The person denies or skips, or the request expires:** the tool is not
  called again, and the model reads the usual note.
- **With `tool_approvals` off,** the reply reaches the model unchanged. Nothing
  ran either way.
- **Before the run continues,** the host tells the tool's service about the
  decision. kaboo-runtime does this in `onDecision`, and never stores a
  decision that carries a `request`. The service, not the agent, decides
  whether the second call runs.

`scopes` always include `once`, even when the tool lists only wider ones: the
person can always approve just the one call, so the service must accept `once`
too. `subject`, `message` and `expiresAt` are optional; without a subject the
request covers the tool. Don't also list such a tool under `interrupt.tools`,
or the person is asked twice.

### Checklist for the service

The agent and the host only carry a request ID. The service is the only one
that can make it mean anything:

1. **IDs:** create random, unguessable request IDs (at least 128 bits) on the
   server.
2. **Binding:** when you create a request, bind it to the person, the
   conversation, the resource (such as the store) and the exact call (a hash of
   the resource and the arguments).
3. **Who may decide:** accept decisions only from the host, over an
   authenticated server-to-server call, never from the browser or the agent.
4. **Which decisions count:** honour a decision only if it's for a request you
   created, for the same person and conversation, still pending, and the first
   one. A repeat of the decision already taken changes nothing and must not
   fail: when the host refuses one decision of a resume, the person answers
   again and every decision of that resume is sent again. Ignore anything else.
5. **What a decision grants:** only a scope the request offered, for the
   subject it named. kaboo offers `once` on every request on top of the scopes
   your reply lists, so accept `once` too. An edited call must keep the same
   subject.
6. **Check again at execution:** the call must match its approval, and normal
   permissions still apply, since roles can change while a request waits.
7. **Waiting and expiry:** a pending request may wait forever but authorizes
   nothing. Remembered approvals expire and can be revoked.
8. **No secrets:** IDs and labels only, because requests are stored in the
   thread and shown on the card.
9. **Audit:** record every decision: who, what, which scope, when.

Lab scenarios:
[approval-plain](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/approval-plain.yaml),
[approval-subagent](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/approval-subagent.yaml),
[approval-grant](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/approval-grant.yaml),
[approval-denied](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/approval-denied.yaml),
[approval-edited](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/approval-edited.yaml)
and
[approval-tool-requested](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/approval-tool-requested.yaml).

## The person can move on

A question or approval waits with no time limit. If the person sends a message
instead of answering, what was waiting is **skipped**: the client answers each
open browser call with "Skipped: the person sent a message instead of
answering" and cancels each open interrupt, the agent closes them, and it
answers the new message (and can ask again). kaboo-react does this when a
message is sent. Lab scenarios:
[question-skipped](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/question-skipped.yaml)
and
[approval-skipped](https://github.com/gl-pgege/kaboo-docs/blob/main/examples/kaboo-workflows-demo/scenarios/approval-skipped.yaml).

## Key behaviours

- **A pause stops the whole run.** A gate or a browser call deep in a delegate
  ends the top-level run, and the answer resumes every level that was waiting.
- **Parallel pauses.** An agent can raise several in one turn, and parallel
  delegates can each ask. Each waits under its own call id, and the run resumes
  once all are answered.
- **Positions.** Questions and approvals work for a plain entry agent and its
  delegates at any depth. Swarm and Graph members go through ag-ui-strands; see
  [Swarm + graph](swarm-and-graph.md) for what they support.

## A pause outlives the process that opened it

A pause can last hours, which makes it the thing most likely to be interrupted
by a deploy. The paused agent's checkpoint, and the place of every delegate it
left paused, travel on the AG-UI state channel in `kaboo_sessions.paused` and
are restored onto whichever process runs the next turn, including one that has
never seen the conversation. A resume therefore works after a restart, on a
second replica, and when the session is rebuilt per run.

Only a resume restores it: a new message supersedes the pause, on a fresh
process as on a warm one, so a host that replays an older state cannot bring
back a gate already answered. A run restores it once: when a hook sends the
agent back for another pass in the same run, the answered gate stays answered
and its tools don't run again. Nothing to configure; see
[Chapter 7](../configuration/Chapter_07.md#pending-interrupts-survive-a-restart-without-a-store)
for the switch that turns it off and why you would.

## Proven by

- `tests/e2e/test_client_tools.py` — the entry agent and a delegate wait for the
  browser and read its answer; the coordinator reads what its delegate asked; a
  delegated question is answered in another process; only allowed agents get a
  browser tool and its context.
- `tests/e2e/test_approvals.py` — a gated call waits and the host is told what
  it waits on; wider scopes only with a host that remembers; approval runs the
  call; a remembered permission runs it without asking; a denial does not run it
  and the agent hears why; a skip does not run it; the decision is recorded on
  the call's result; a message sent while a request waits starts a new turn.
- `tests/runtime/test_approval_gate.py` — matching, scopes, subjects, answers,
  deadlines and the host-facing descriptors.
- `tests/e2e/test_tool_approvals.py` — a tool's reply pauses the run with the
  tool's own request (every reply shape); approving calls the tool exactly once
  more, with edits; a denial or skip never calls it again; an approval the
  service never heard of doesn't loop; a decision for the chat covers only the
  store it named; the reply never reaches the model and a forged request
  changes nothing; with the flag off the reply reaches the model.
- `tests/e2e/test_cross_cutting.py::test_an_interrupt_then_resume`
  (plain / delegate / swarm / graph positions).
- `tests/e2e/test_complex.py::test_parallel_interrupts_surface_together_and_resume`
  (two gates in one step, distinct ids, both resumed).
- `tests/e2e/test_cross_cutting.py::test_approval_survives_a_restart_of_the_service`
  (the resuming turn runs on a fresh process that never saw the question).
- `tests/e2e/test_runtime_configs.py::test_approval_survives_the_session_being_rebuilt`
  (the same, within one process, when every run builds its own session).
- `tests/runtime/test_session_state_hook.py::test_a_later_invocation_in_the_run_does_not_bring_back_the_answered_gate`
  and `::test_the_adapter_and_the_hook_restore_once_between_them` (a run restores
  the gate once, whichever of the adapter and the hook gets there first).
