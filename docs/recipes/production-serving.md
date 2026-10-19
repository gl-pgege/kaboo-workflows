# Serve on AgentCore in production

`create_agui_app` already speaks AgentCore Runtime's HTTP contract: AG-UI on
`POST /invocations`, a health check on `GET /ping`. This recipe covers what a
production deployment adds on top — the settings worth turning on, and four
behaviours of the host that the defaults do not account for.

## The app

```{.python notest}
import os
from pathlib import Path

import strands.models.bedrock as bedrock

from kaboo_workflows.adapters import create_agui_app

from my_service.auth import verify_run_token

bedrock.DEFAULT_READ_TIMEOUT = int(os.environ.get("BEDROCK_READ_TIMEOUT", "300"))

app = create_agui_app(
    Path(__file__).parent / "config.yaml",
    session_config_key="workflow_config",
    allowed_mcp_hosts=["gateway.internal.example.com"],
    auth=verify_run_token,
    ping_path=None,
    activity_deltas=True,
    response_budget=CONTINUATION_BUDGET,
)
```

- **`session_config_key`** lets each run bring its own workflow, layered over
  the base config — see [Compose a workflow per run](per-run-workflows.md).
  Without it, the service serves its own config, and each conversation gets its
  own agents the first time it runs (the 256 most recent are kept), so one
  person's sub-agent paused on a question never turns up in another's chat.
- **`allowed_mcp_hosts`** bounds what a submitted workflow's MCP clients may
  connect to. Set it whenever workflows are authored anywhere but your
  repository: an arbitrary URL needs no code to exfiltrate data.
- **`auth`** verifies every request and binds the caller, so MCP tools can run
  as them — see [Call MCP tools as the signed-in user](user-identity-mcp.md).
- **`ping_path=None`** because the packaged health check always answers
  healthy, and AgentCore needs more than that (below).
- **`activity_deltas`** sends activity as JSON Patches after the first
  snapshot. Full snapshots on every change are what fill a response on long,
  multi-agent runs.
- **The Bedrock read timeout** defaults to strands' value, which a long
  thinking turn can exceed. Raise it for the slowest call you expect.

AgentCore Runtime expects the container on port 8080.

## Report busy while a run is open

AgentCore decides a session is idle from its `/ping` status, not from whether a
request is still on the wire. A session that only ever answers `Healthy` can be
reaped mid-run, which reaches the user as a stream that ends with no terminal
event. Answer `HealthyBusy` while any invocation is open:

```{.python notest}
class RunCounter:
    def __init__(self) -> None:
        self.active = 0

    @property
    def status(self) -> str:
        return "HealthyBusy" if self.active else "Healthy"


runs = RunCounter()


@app.get("/ping")
def ping() -> dict[str, str]:
    return {"status": runs.status}
```

Increment `runs.active` when a `POST /invocations` arrives and decrement it when
the response ends — in the ASGI middleware below, which sees both.

## Stay under the response cap

AgentCore closes the socket once a response reaches 100 MiB, with no error of
its own. The run dies without a terminal event, and CopilotKit reports
whichever tool call was open as failed — the user is told a healthy tool broke.
Two budgets keep that from happening:

1. **`response_budget`** pauses the run at the next tool boundary once the
   response has carried that many bytes, and continues the same turn in a fresh
   response. The user loses nothing. Clients resolve the pause on their own; it
   is an interrupt marked `reason: "continuation"`
   ([Human-in-the-loop](../workflows/human-in-the-loop.md)).
2. **A hard backstop** for a response that fills without ever reaching a tool
   call: wrap the ASGI `send` so it forwards whole SSE frames, and once the
   budget is spent, close the stream with a real `RUN_FINISHED` and a short
   message saying the answer was cut off. A truncated answer with a reason
   beats a failure with none.

Give the pause room to fire first:

```{.python notest}
AGENTCORE_RESPONSE_CAP = 104_857_600
HARD_BUDGET = AGENTCORE_RESPONSE_CAP - 10 * 1024 * 1024
CONTINUATION_BUDGET = HARD_BUDGET * 3 // 4
```

The margin under the cap leaves room for the closing events and for
chunked-transfer framing, which counts against the cap but is not yours to
measure. When cutting the stream, split only on the blank line between SSE
frames — cutting an arbitrary chunk leaves the client half an event.

## Non-streaming calls over `/invocations`

AgentCore Runtime forwards only `POST /invocations`. Anything else your service
answers — validating a workflow before an admin saves it, running a workflow
once for a background job — has to ride that path too. Dispatch on a field in
the body, in a **pure ASGI** middleware, and replay the body downstream for
ordinary AG-UI runs:

```{.python notest}
class NonStreamingOverInvocations:
    def __init__(self, app, runs: RunCounter) -> None:
        self.app = app
        self.runs = runs

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] != "/invocations" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        self.runs.active += 1
        try:
            body = await read_body(receive)
            if b'"my_validate"' in body[:4096]:
                await answer_validation(body, scope, send)
                return
            await self.app(scope, replay(body, receive), budgeted(send, body))
        finally:
            self.runs.active -= 1


app.add_middleware(NonStreamingOverInvocations, runs=runs)
```

Starlette's `BaseHTTPMiddleware` will not do here: once it has consumed the
body, its disconnect listener receives the replayed `http.request` and breaks
the downstream SSE stream. Authenticate the non-streaming branches with the
same verifier the AG-UI path uses.

## Validate a workflow before saving it

`load_session_config` runs the same overlay, schema and reference checks a run
would, without building anything:

```{.python notest}
from kaboo_workflows import load_session_config, parse_config_sources

BASE_RAW = parse_config_sources("config.yaml")


def validate(workflow_yaml: str) -> dict:
    try:
        config = load_session_config(
            BASE_RAW, workflow_yaml, allowed_mcp_hosts=["gateway.internal.example.com"]
        )
    except Exception as exc:
        return {"valid": False, "error": str(exc)}
    return {"valid": True, "entry": config.entry, "agents": sorted(config.agents)}
```

The error names the bad reference and lists what is available, which makes it
a good message to show an admin. Add your own rules after it. Ours refuse an
agent holding more than 20 tools, a tool granted twice by overlapping MCP
clients, and a prompt that names a tool the agent is not granted.

## Run a workflow once, without streaming

For a background job, build the run the same way a streamed run is built and
answer with the final text:

```{.python notest}
from dataclasses import replace

from kaboo_workflows import (
    load_session,
    load_session_config,
    resolve_infra,
    resolve_run_clients,
    set_auth_context,
    validate_raw_config,
)

BASE_INFRA = resolve_infra(validate_raw_config(BASE_RAW))


async def run_once(workflow_yaml: str, prompt: str, principal) -> str:
    config = load_session_config(BASE_RAW, workflow_yaml, allowed_mcp_hosts=ALLOWED)
    set_auth_context(principal)
    clients = resolve_run_clients(config, BASE_INFRA)
    clients.start(pin_clients=False)
    try:
        resolved = load_session(
            config, replace(BASE_INFRA, clients=clients.clients, mcp_lifecycle=clients)
        )
        result = await resolved.entry.invoke_async(prompt)
        return str(result).strip()
    finally:
        clients.stop()
```

`set_auth_context` comes first so relayed MCP calls go out as the caller, and
the run's clients stop with the call, as they do with a streamed run.

## Related

- [Chapter 17: when the config itself is per-request](../configuration/Chapter_17.md#when-the-config-itself-is-per-request)
- [Evaluation: enable telemetry](../evaluation.md#enable-telemetry) for traces
  in Langfuse or any OTLP backend
- `examples/agentcore/` for inbound auth styles on AgentCore
