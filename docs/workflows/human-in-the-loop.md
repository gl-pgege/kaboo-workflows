# Human-in-the-loop

An agent can pause a run for the person in two ways, both on the same
mechanism: it **asks a question** with the built-in `ask_user` tool, or a
**gated tool waits for approval** before it runs. The run resumes with the
person's answer — minutes or days later, on any replica — and the agent carries
on mid-thought.

```
review_team (delegate):
  coordinator ─▶ field_agent (interrupt: true, asks the user)
```

## Config

```yaml
agents:
  field_agent:
    interrupt: true          # grants the built-in ask_user tool
    system_prompt: |
      Before finalizing anything risky, call ask_user to confirm. If several
      details are unclear, ask them together in one ask_user call.

orchestrations:
  review_team:
    mode: delegate
    entry_name: coordinator
    connections:
      - { agent: field_agent, description: "Gated field work with user confirmation" }

entry: review_team
```

Full runnable project: [`examples/18_hitl`](https://github.com/gl-pgege/kaboo-workflows/tree/main/examples/18_hitl).

## Questions are multiple choice

`ask_user` asks one or more questions, each with two or more options — like
the question cards in Cursor:

- `multiple` makes a question multi-select.
- The person can always pick **Other** and answer in their own words; the card
  adds it, so the agent never lists "Other".
- A question without enough options is refused with a tool error that teaches
  the rule, so the model asks again properly instead of reaching the person.

```{.python notest}
ask_user(question="Which market?", options=["AI", "Cloud", "IoT"])
ask_user(questions=[
    {"question": "Which market?", "options": ["AI", "Cloud"]},
    {"question": "Which sectors?", "options": ["B2B", "B2C"], "multiple": True},
])
```

The answer comes back as a `{question: answer}` map: a string, a list for a
multi-select question, or the person's own words for Other. A skipped question
returns "The person skipped this question." App-specific inputs — a file or a
data source — are not questions: raise your own request kind
([Ask the user](../recipes/asking-the-user.md)).

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
      ask_user: false
```

- **`tools`** — names, `fnmatch` globs, or `{name, remember, subject}` objects.
  The first match applies; `ask_user` is never gated.
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
  deadline does not run the call. Without it, approvals wait like questions.

An approval request's `metadata` carries `tool_name`, `tool_input`, `subject`
(`key`, `label`), `scopes` (what the person may choose), with a deadline
`expiresAt`, and `request` when a tool asked for the approval itself
([below](#tools-that-enforce-their-own-approvals)).

## Answers

On pause the run finishes with an `interrupt` outcome listing the open requests
(each with an `id`). The client resumes by answering each of them:

| The person… | Resume entry | The agent |
|---|---|---|
| approves | `{"status": "resolved", "payload": {"status": "approved", "scope"?: "once" \| "thread" \| "always", "tool_input"?: {...}}}` | runs the call, with the person's edit if given |
| denies | `{"status": "resolved", "payload": {"status": "cancelled", "reason"?: "..."}}` | cancels it; the model reads "Declined: <reason>" |
| answers a question | `{"status": "resolved", "payload": {"<question>": "<answer>"}}` | returns the answers to the model |
| skips | `{"status": "cancelled"}` | cancels it; the model reads "Skipped: …" |

Only an `approved` answer runs a gated call; anything else does not. A request
the resume leaves out counts as skipped, never as approved. A denial is sent as
`cancelled` so an agent that predates denial reasons cannot mistake it for an
approval.

## Remembered approvals

"Allow for this chat" and "Always allow" are opt-in on both sides:

- **The agent** offers them when its `remember` allows, and only when the host
  remembers decisions: the host sends `forwardedProps.kaboo_grants`, and the run
  publishes its open requests in `kaboo_session.approvals` (on by default; see
  `runtime.persist_session_state`).
- **The host** saves the person's decision and sends the permissions on every
  run as `kaboo_grants: [{tool, subject, scope}]`. A call matching one runs
  without asking. kaboo-runtime does this when given a grant store.

The agent never stores a decision itself. The person's own answer on a request
wins over a permission that answer created, so an edit still applies. The
tool's activity entry records how each gated call was decided:
`approval: {decision, by: "person" | "permission", scope?, reason?}`.

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

## The person can move on

A question or approval waits with no time limit. If the person sends a message
instead of answering, what was waiting is **skipped**: the open calls close as
"Skipped: the person sent a message instead of answering", their activity cards
close, and the agent answers the new message (and can ask again). This holds
for a plain new message and for the AG-UI form — a resume that cancels every
open request and ends with the new message, which kaboo-react sends.

## Key behaviours

- **Interrupts bubble up.** A gate fired by a nested sub-agent surfaces to the
  single top-level handler and pauses the entire run — no matter how deep it is.
- **Parallel gated tool calls.** An agent can raise several gates in one turn
  (a multi-question form, or two tool calls). Each becomes its own interrupt with
  a distinct tool-call id, and the run resumes once all are answered.
- **Positions.** HITL works for a plain entry agent, a delegate sub-agent, and
  swarm/graph nodes alike.

## Run

HITL needs a client with a resume UI — serve it and connect a CopilotKit
frontend:

```bash
OPENROUTER_API_KEY=... uv run kaboo-serve examples/18_hitl/config.yaml
```

## A gate outlives the process that opened it

An approval is the one pause that can last hours, which makes it the pause most
likely to be interrupted by a deploy. The gate itself lives in the agent object's
interrupt state, so a restart between the question and the answer used to strand
it: the user clicked approve and got `No agent session found for resume`.

Served through `create_agui_app`, the pending gate now travels on the AG-UI state
channel under `kaboo_session` and is restored onto whichever agent runs the
resume — including one that has never seen the conversation. A resume therefore
works after a restart, on a second replica, and when the session is rebuilt per
run. Only a resume restores it: a new message supersedes the gate, on a fresh
process as on a warm one, so a host that replays an older snapshot cannot bring
back a gate already answered. Nothing to configure; see
[Chapter 7](../configuration/Chapter_07.md#pending-interrupts-survive-a-restart-without-a-store)
for the switch that turns it off and why you would.

The exception is a Swarm or Graph *entry*: strands does not yet persist state for
orchestration node agents, so a multi-agent entry still needs the process to stay
up between question and answer. A plain agent, with or without delegates, is
covered.

## A pause nobody answers

Not every interrupt is a question. When a host caps the size of one HTTP
response, `create_agui_app(response_budget=...)` uses the same machinery to
split a large turn across two: the run stops at the next tool call, the
response closes properly, and the client resumes at once. The interrupt is
marked `reason: "continuation"` so a client can tell it apart and resolve it
without showing anyone a prompt. Everything above still applies — the tool runs
once, the state travels on `kaboo_session`, a different replica can pick it up
— and the turn id is unchanged, so the user sees one turn. See
[concepts](../concepts.md#runs-larger-than-one-response).

## Proven by

- `tests/e2e/test_approvals.py` — a gated call waits and the host is told what
  it waits on; wider scopes only with a host that remembers; approval runs the
  call; a remembered permission runs it without asking; a denial does not run it
  and the agent hears why; a skip does not run it; the decision is recorded on
  the tool's activity entry; a message sent while a request waits starts a new
  turn.
- `tests/runtime/test_approval_gate.py` — matching, scopes, subjects, answers,
  deadlines and the host-facing descriptors.
- `tests/e2e/test_tool_approvals.py` — a tool's reply pauses the run with the
  tool's own request (every reply shape); approving calls the tool exactly once
  more, with edits; a denial or skip never calls it again; an approval the
  service never heard of doesn't loop; a decision for the chat covers only the
  store it named; the reply never reaches the model and a forged request
  changes nothing; with the flag off the reply reaches the model; a sub-agent's
  decision is recorded on its activity entry.
- `tests/runtime/test_tool_approvals.py` — reading replies, and which tools the
  hook wraps.
- `tests/runtime/test_ask_user.py` and
  `tests/e2e/test_cross_cutting.py::test_a_free_text_question_is_refused_and_the_model_asks_with_options`.
- `tests/e2e/test_cross_cutting.py::test_a_full_response_pauses_the_run_and_the_next_one_finishes_it`
  (the budget pauses an ungated tool call and the resume completes it, once).
- `tests/e2e/test_cross_cutting.py::test_ask_user_interrupt_then_resume`
  (plain / delegate / swarm / graph positions).
- `tests/e2e/test_complex.py::test_parallel_interrupts_surface_together_and_resume`
  (two gates in one step, distinct ids, both resumed).
- `tests/e2e/test_cross_cutting.py::test_approval_survives_a_restart_of_the_service`
  (the resuming turn runs on a fresh process that never saw the question).
- `tests/e2e/test_runtime_configs.py::test_approval_survives_the_session_being_rebuilt`
  (the same, within one process, when every run builds its own session).
