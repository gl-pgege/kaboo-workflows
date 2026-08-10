# Chapter 7: Session Persistence — Memory That Survives Restarts

[← Back to Table of Contents](README.md) | [← Previous: Hooks](Chapter_06.md)

---

By default, agents are stateless — each `load()` call starts fresh. The `session_manager` section enables persistent conversation history.

Pending human-in-the-loop interrupts are a separate concern and are handled for you — see [Pending interrupts survive a restart without a store](#pending-interrupts-survive-a-restart-without-a-store) at the end of this chapter.

## Global Session Manager

Define a session manager at the root level and **every agent** inherits it:

```yaml
session_manager:
  provider: file
  params:
    storage_dir: ./.sessions
    session_id: my-session-001

agents:
  assistant:
    model: default
    system_prompt: "You remember everything."

entry: assistant
```

## Built-in Providers

| Provider | Backend | Required Package |
|----------|---------|------------------|
| `file` | Local filesystem | *(included)* |
| `s3` | Amazon S3 bucket | *(included, needs AWS creds)* |
| `agentcore` | Bedrock AgentCore Memory | `pip install kaboo-workflows[agentcore-memory]` |

### File Provider

```yaml
session_manager:
  provider: file
  params:
    storage_dir: ./.sessions
    session_id: my-session
```

Sessions are stored as files in `storage_dir`. Delete the directory to start fresh.

### S3 Provider

```yaml
session_manager:
  provider: s3
  params:
    bucket_name: my-agent-sessions
    session_id: prod-session-001
```

Requires AWS credentials in the environment.

### AgentCore Provider

The `agentcore` provider requires a unique `actor_id` per agent and **cannot** be set globally — set it per-agent instead:

```yaml
agents:
  assistant:
    model: default
    system_prompt: "You are helpful."
    session_manager:
      provider: agentcore
      params:
        actor_id: assistant
        memory_id: my-memory-store
```

## Per-Agent Session Manager

Any agent can override the global session manager with its own:

```yaml
session_manager:
  provider: file
  params:
    storage_dir: ./.sessions

agents:
  persistent_agent:
    model: default
    system_prompt: "I remember."
    # Inherits the global file session manager

  stateless_agent:
    model: default
    system_prompt: "I forget."
    session_manager: ~             # <-- Explicit opt-out with YAML null (~)
```

Setting `session_manager: ~` (YAML null) on an agent **explicitly opts it out** of the global default. This is important — without this, it would inherit the global one.

## Session ID Resolution

When no `session_id` is provided, kaboo-workflows generates a random UUID — meaning each run gets a fresh session. The resolution order is:

1. **Runtime override** — via `load_session(..., session_id="abc")`
2. **`params.session_id`** — from YAML config
3. **Random UUID** — fresh session per run

## Custom Session Manager

For anything beyond the built-in providers, point `type` to your own class:

```yaml
session_manager:
  type: my_package.sessions:RedisSessionManager
  params:
    host: localhost
    port: 6379
```

The class must be a subclass of `strands.session.SessionManager`. When `type` is set, `provider` is ignored.

## Swarm Agents and Sessions

**Important limitation**: agents that participate in a Swarm orchestration **cannot** have a session manager. This is a strands-agents limitation. If a global session manager is set and an agent is used in a swarm, kaboo-workflows will raise a clear error:

```
ConfigurationError: Agent 'drafter' is in swarm orchestration and cannot
have a session manager (source: global 'session_manager:' in config).
Fix: Add 'session_manager: ~' to agent 'drafter' to opt out of the global default.
```

The fix: add `session_manager: ~` to each swarm agent to opt out.

> **Tips & Tricks**
>
> - For development, `file` provider with a fixed `session_id` is great — restart your script and the agent remembers your conversation.
> - For server/API deployments, use `load_session()` with a per-request `session_id`. kaboo-workflows
>   computes a single `effective_session_id` from your value and threads it to every agent and
>   orchestration, so all agents in one request share the same session folder. See
>   [the multi-tenant pattern](#the-multi-tenant-server-pattern) below.
> - Delete the `.sessions/` directory to "factory reset" your agent's memory.

## The Multi-Tenant Server Pattern

For web servers where each HTTP request needs its own session:

```{.python notest}
from kaboo_workflows import load_config, resolve_infra, load_session

# Once at startup
app_config = load_config("config.yaml")
infra = resolve_infra(app_config)
infra.mcp_lifecycle.start()

# Per request
def handle_request(user_session_id: str, message: str):
    resolved = load_session(app_config, infra, session_id=user_session_id)
    return resolved.entry(message)
```

MCP servers are shared across sessions (started once), but agents and their conversation state are created fresh per session.

## Pending Interrupts Survive a Restart Without a Store

A session manager persists the *conversation*. An interrupt is different: when an agent pauses on `ask_user` or a gated tool, what has to survive is the open gate, and it lives in the agent object's own interrupt state. That object is per-process, so before this existed a restart between the question and the answer stranded the approval — the user clicked approve and got "No agent session found for resume".

There is nothing to configure. Serving through `create_agui_app`, the pending interrupt travels on the AG-UI **state channel** under `kaboo_session`, the same channel that carries `kaboo_history`:

- On the way out, it is written into the `STATE_SNAPSHOT` your host already persists.
- On the way in, it is read from `RunAgentInput.state` and restored onto the agent that runs the turn — including one that has never seen the conversation, which is how a resume works after a restart, on a second replica, or when the session is rebuilt per run.

Hosts on [kaboo-runtime](https://github.com/gl-pgege/kaboo-runtime) get this end to end for free, because the runtime persists state snapshots per thread and replays them into the next run.

Turn it off in the one case where it would be wrong:

```yaml
runtime:
  persist_session_state: false
```

The gate arrives from the client, so trusting it means trusting the client. That is correct when the AG-UI endpoint is called by your own server (the supported topology — a browser talks to your API, your API talks to kaboo), and wrong if you expose `/invocations` straight to a browser, where a user could hand you back a gate you never issued.

Two details worth knowing:

- A warm agent's own state wins over the incoming copy, so a stale snapshot cannot resurrect a gate that was already answered.
- Swarm and Graph entries are not covered: strands does not yet support session persistence for orchestration node agents, so a multi-agent entry still relies on the process staying up. A plain-agent entry — including one with delegates — is fully covered.

---

[Next: Chapter 8 — Conversation Managers →](Chapter_08.md)
