"""AG-UI server — expose YAML-defined agents as AG-UI SSE endpoints.

Bridges kaboo-workflows (YAML -> strands.Agent) with ag-ui-strands
(strands.Agent -> AG-UI SSE). This is the primary serving mechanism
for kaboo-workflows, producing CopilotKit-compatible event streams.

Usage::

    from kaboo_workflows.adapters import create_agui_app

    app = create_agui_app("config.yaml")
    # Run with: uvicorn module:app --port 8080
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ag_ui_strands import StrandsAgent
from ag_ui_strands.endpoint import add_ping, add_strands_fastapi_endpoint
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from strands import Agent
from strands.multiagent.base import MultiAgentBase

from kaboo_workflows import load

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


def create_agui_app(
    config_path: str | Path,
    *,
    endpoint: str = "/invocations",
    ping_path: str | None = "/ping",
) -> FastAPI:
    """Create a FastAPI app serving AG-UI SSE from a YAML config.

    Args:
        config_path: Path to the kaboo-workflows YAML config.
        endpoint: Path for the AG-UI agent endpoint.
        ping_path: Path for the health check endpoint. None to disable.

    Returns:
        A FastAPI application with AG-UI SSE streaming.

    Raises:
        TypeError: If the entry node is an unsupported orchestration type.
    """
    config_path = Path(config_path).resolve()
    resolved = load(str(config_path))

    entry = resolved.entry
    mcp_lifecycle = resolved.mcp_lifecycle

    if isinstance(entry, MultiAgentBase):
        _entry_agent = _wrap_orchestration(entry)
    elif isinstance(entry, Agent):
        _entry_agent = entry
    else:
        raise TypeError(f"Unsupported entry node type: {type(entry)}")

    agui_agent = StrandsAgent(
        agent=_entry_agent,
        name=_resolve_entry_name(entry, resolved),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("AG-UI server starting — MCP lifecycle already active")
        try:
            yield
        finally:
            logger.info("AG-UI server shutting down — stopping MCP lifecycle")
            mcp_lifecycle.stop()

    app = FastAPI(
        title="kaboo-workflows",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    add_strands_fastapi_endpoint(app, agui_agent, endpoint)
    if ping_path is not None:
        add_ping(app, ping_path)

    return app


def _resolve_entry_name(entry: Any, resolved: Any) -> str:
    """Determine the human-readable name for the entry node."""
    for name, agent in resolved.agents.items():
        if agent is entry:
            return name
    for name, orch in resolved.orchestrators.items():
        if orch is entry:
            return name
    return "entry"


def _wrap_orchestration(orchestration: Any) -> Any:
    """Extract a strands.Agent from a MultiAgentBase orchestration.

    StrandsAgent requires a strands.Agent. For orchestrations (Swarm, Graph,
    etc.), we extract the manager/agent which is a plain Agent that drives
    the orchestration via tool-based delegation.
    """
    manager = getattr(orchestration, "manager", None)
    if manager is not None:
        logger.info(
            "Wrapping orchestration manager agent for AG-UI (type=%s)",
            type(orchestration).__name__,
        )
        return manager

    agent_attr = getattr(orchestration, "agent", None)
    if agent_attr is not None:
        return agent_attr

    raise TypeError(
        f"Cannot extract a strands.Agent from orchestration type "
        f"'{type(orchestration).__name__}'. AG-UI requires a plain Agent. "
        f"Use a single-agent entry or a Delegate orchestration with a manager."
    )
