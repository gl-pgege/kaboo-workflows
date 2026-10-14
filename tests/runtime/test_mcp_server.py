"""A managed MCPServer serves real MCP over HTTP: start, list, call, stop."""

from __future__ import annotations

import socket

import pytest

from kaboo_workflows.mcp import create_mcp_client, create_mcp_server
from kaboo_workflows.mcp.transports import MCP_SERVER_TRANSPORT


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


def test_streamable_http_server_round_trip() -> None:
    server = create_mcp_server(name="calc", tools=[add], port=_free_port())
    server.start()
    try:
        assert server.wait_ready(timeout=10)
        client = create_mcp_client(server=server)
        with client:
            assert [tool.tool_name for tool in client.list_tools_sync()] == ["add"]
            result = client.call_tool_sync("t1", "add", {"a": 2, "b": 3})
        assert result["status"] == "success"
        assert result["content"] == [{"text": "5.0"}]
    finally:
        server.stop()
    assert not server.is_running


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_server_starts_and_stops(transport: MCP_SERVER_TRANSPORT) -> None:
    server = create_mcp_server(name="calc", tools=[add], port=_free_port(), transport=transport)
    server.start()
    try:
        assert server.wait_ready(timeout=10)
    finally:
        server.stop()
    assert not server.is_running
