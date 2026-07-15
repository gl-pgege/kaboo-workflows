"""Resolve MCPServerDef, MCPClientDef, and tool specs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ...mcp.client import create_mcp_client
from ...mcp.server import MCPServer
from ...mcp.transports import MCP_TRANSPORT
from ...tools import resolve_tool_specs
from ...utils import load_object

if TYPE_CHECKING:
    from strands.tools.mcp import MCPClient as StrandsMCPClient

    from ..schema import MCPClientAuthDef, MCPClientDef, MCPServerDef


def resolve_tools(tool_specs: list[str]) -> list[Any]:
    """Resolve tool specification strings to tool objects.

    Delegates to :func:``~kaboo_workflows.tools.resolve_tool_specs``,
    which understands module paths, file paths, and directory paths.

    Args:
        tool_specs: List of tool specification strings.

    Returns:
        Flat list of tool objects.
    """
    return resolve_tool_specs(tool_specs)


def resolve_mcp_server(
    server_def: MCPServerDef,
    *,
    name: str = "",
) -> MCPServer:
    """Resolve an MCPServerDef to an MCPServer instance.

    Imports the factory from the full import path and passes
    ``server_def.params`` as constructor kwargs.

    Args:
        server_def: MCP server definition from YAML.
        name: Server name (key under ``mcp_servers:``).

    Returns:
        Instantiated MCPServer (not yet started).

    Raises:
        ValueError: If the server type cannot be resolved.
        TypeError: If the resolved object is not an MCPServer subclass.
    """
    factory = load_object(server_def.type, target="MCP server")
    server = factory(name=name, **server_def.params)
    if not isinstance(server, MCPServer):
        raise TypeError(
            f"MCP server factory '{server_def.type}' returned {type(server).__name__}, "
            f"expected MCPServer subclass."
        )
    return server


def resolve_mcp_client(
    client_def: MCPClientDef,
    servers: dict[str, MCPServer],
    *,
    name: str = "",
) -> StrandsMCPClient:
    """Resolve an MCPClientDef to a strands MCPClient.

    Uses :func:``~kaboo_workflows.mcp.client.create_mcp_client``.
    Resolves server reference to actual MCPServer instance.

    Args:
        client_def: MCP client definition from YAML.
        servers: Already-resolved server instances by name.
        name: Client name (key under ``mcp_clients:``).

    Returns:
        A strands MCPClient instance (not started).

    Raises:
        ValueError: If a server reference cannot be resolved.
    """
    server: MCPServer | None = None
    if client_def.server:
        if client_def.server not in servers:
            raise ValueError(
                f"MCP client '{name}' references server '{client_def.server}' "
                f"which is not defined under mcp_servers:.\n"
                f"Available: {', '.join(sorted(servers)) or '(none)'}"
            )
        server = servers[client_def.server]

    transport_options = _apply_client_auth(client_def, server)

    kwargs: dict[str, Any] = {
        "server": server,
        "url": client_def.url,
        "command": client_def.command,
        "transport_options": transport_options or None,
        **client_def.params,
    }
    if client_def.transport is not None:
        kwargs["transport"] = cast(MCP_TRANSPORT, client_def.transport)
    return create_mcp_client(**kwargs)


def _apply_client_auth(
    client_def: MCPClientDef,
    server: MCPServer | None,
) -> dict[str, Any]:
    """Return ``transport_options`` with any declarative ``auth:`` wired in.

    Builds the strategy from :class:`~kaboo_workflows.config.schema.MCPClientAuthDef`
    and attaches it for the effective transport (``auth`` for SSE, a dedicated
    ``httpx.AsyncClient`` for streamable-http). Returns the options unchanged
    when the client declares no ``auth:``.
    """
    options = dict(client_def.transport_options or {})
    if client_def.auth is None:
        return options

    from ...auth import apply_auth_to_transport_options, build_auth

    auth_def = cast("MCPClientAuthDef", client_def.auth)
    auth_obj = build_auth(auth_def.type, auth_def.params)
    return apply_auth_to_transport_options(
        options, auth_obj, transport=_effective_transport(client_def, server)
    )


def _effective_transport(client_def: MCPClientDef, server: MCPServer | None) -> str:
    """Resolve the transport used for auth wiring (explicit or URL-detected)."""
    if client_def.transport is not None:
        return client_def.transport
    url = client_def.url or (getattr(server, "url", None) if server is not None else None)
    if url:
        from ...mcp.client import _detect_transport

        return _detect_transport(url)
    return "streamable-http"
