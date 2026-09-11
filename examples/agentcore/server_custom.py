"""agentcore — custom AgentCore-compatible server with per-request MCP auth.

The escape hatch for full control and true multi-tenant isolation. Instead of
``create_agui_app`` (one shared MCP client at boot), this builds infrastructure
once and then, per request, injects an MCP client bound to *that caller's*
identity before calling ``load_session``.

Why per request: strands snapshots the context via ``contextvars.copy_context()``
when an MCP client *starts*. Creating the client inside the request (so it starts
in the request's context, or binding the token explicitly at construction) is
what makes ``relay`` carry the right per-user token in a process that serves
many users concurrently.

This exposes the AgentCore Runtime contract (``/ping`` + ``/invocations``), so it
can be containerized and deployed to AgentCore Runtime or any HTTP host.

Run:
    uv run uvicorn examples.agentcore.server_custom:app --port 8080
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from kaboo_workflows import (
    RelayTokenAuth,
    create_mcp_client,
    load_config,
    load_session,
    resolve_infra,
)

CONFIG = Path(__file__).parent / "config.yaml"
GATEWAY_URL = os.environ["GATEWAY_URL"]

# Build + start shared infrastructure once. Models and the MCP lifecycle are
# shared; the per-request client below is created fresh each turn.
app_config = load_config(CONFIG)
infra = resolve_infra(app_config)
infra.mcp_lifecycle.start()

app = FastAPI(title="kaboo-workflows agentcore")


@app.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/invocations")
async def invocations(request: Request) -> StreamingResponse:
    # AgentCore injects the caller's identity; here we bind it explicitly to a
    # per-request MCP client so the token relayed is THIS user's.
    workload_token = request.headers.get("WorkloadAccessToken")
    if not workload_token:
        raise HTTPException(status_code=401, detail="missing workload token")

    body = await request.json()

    # Per-request, per-user MCP client: the relay strategy is bound to this
    # caller's token explicitly (no reliance on cross-thread context). If the
    # gateway needs a different token downstream, it exchanges for one itself
    # — that is not this process's job.
    infra.clients["gateway"] = create_mcp_client(
        url=GATEWAY_URL,
        transport="streamable-http",
        transport_options={
            "http_client": httpx.AsyncClient(auth=RelayTokenAuth(token=workload_token))
        },
    )

    resolved = load_session(app_config, infra, session_id=body.get("session_id"))

    async def stream() -> AsyncIterator[str]:
        async for event in resolved.entry.stream_async(body["message"]):
            yield f"data: {event}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
