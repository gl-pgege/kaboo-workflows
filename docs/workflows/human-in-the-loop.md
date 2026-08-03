# Human-in-the-loop

Set `interrupt: true` on an agent to grant it the built-in `ask_user` tool. When
the agent calls it, the run pauses and the question surfaces to the client; on
resume the same call returns the user's answer.

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

## Tool gates and expiry

Beyond `ask_user`, `interrupt.tools` lists tool names that pause for approval
*before executing*. `interrupt.ttl_seconds` optionally stamps an `expiresAt`
ISO-8601 timestamp onto every gate interrupt (AG-UI `Interrupt.expiresAt`), so
clients can render a countdown and servers can expire unanswered approvals —
kaboo itself never auto-expires; enforcement stays with the caller.

```yaml
agents:
  field_agent:
    interrupt:
      tools: [transition_work_item, delete_work_item]
      ask_user: false
      ttl_seconds: 86400   # each gate carries expiresAt = now + 24h
```

## Resume protocol

On pause, the run finishes with an `interrupt` outcome listing the pending
interrupts (each with an `id`). The client resumes by sending, per interrupt,
either `{"status": "resolved", "payload": ...}` or `{"status": "cancelled"}`
(see [errors & rejection](errors-and-rejection.md)).

For tool gates, a resolved payload of `{"status": "approved", "tool_input":
{...}}` executes the gated call with the user's edited arguments instead of
the agent's — approving a subset of a bulk operation is one resume, not a
round-trip through the model. A payload of `{"status": "cancelled"}` rejects
the call just like a cancelled entry.

## Proven by

- `tests/e2e/test_cross_cutting.py::test_ask_user_interrupt_then_resume`
  (plain / delegate / swarm / graph positions).
- `tests/e2e/test_complex.py::test_parallel_interrupts_surface_together_and_resume`
  (two gates in one step, distinct ids, both resumed).
