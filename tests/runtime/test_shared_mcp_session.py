"""A run's MCP clients that connect the same way share one session."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kaboo_workflows.config.resolvers import resolve_infra, resolve_run_clients
from kaboo_workflows.config.schema import AppConfig
from kaboo_workflows.mcp.client import ToolView


def _config(clients: dict[str, Any]) -> AppConfig:
    return AppConfig.model_validate(
        {
            "mcp_clients": clients,
            "agents": {
                "a": {
                    "model": {
                        "provider": "kaboo_workflows.testing:ScriptedModel",
                        "model_id": "scripted",
                    }
                }
            },
            "entry": "a",
        }
    )


def _client(url: str, *allowed: str) -> dict[str, Any]:
    return {
        "url": url,
        "transport": "streamable-http",
        "params": {"tool_filters": {"allowed": list(allowed)}},
    }


def test_clients_that_differ_only_in_their_filters_share_one_connection() -> None:
    config = _config(
        {
            "dashboard": _client("http://gw/mcp", "dashboard___open"),
            "snapshot": _client("http://gw/mcp", "dashboard___snapshot"),
            "other": _client("http://elsewhere/mcp", "search"),
        }
    )
    clients: dict[str, Any] = resolve_run_clients(config, resolve_infra(config)).clients

    assert clients["dashboard"].shared is clients["snapshot"].shared
    assert clients["other"].shared is not clients["dashboard"].shared


class _Shared:
    def __init__(self, names: list[str]) -> None:
        self._tools = [
            SimpleNamespace(tool_name=n, mcp_tool=SimpleNamespace(name=n)) for n in names
        ]

    async def load_tools(self, **_kwargs: Any) -> list[Any]:
        return self._tools


async def test_each_view_offers_only_its_own_clients_tools() -> None:
    shared = _Shared(["dashboard___open", "dashboard___snapshot", "sql___run"])
    open_only = ToolView(shared, {"allowed": ["dashboard___open"]})  # ty: ignore[invalid-argument-type]
    no_sql = ToolView(shared, {"rejected": ["sql___run"]})  # ty: ignore[invalid-argument-type]

    assert [t.tool_name for t in await open_only.load_tools()] == ["dashboard___open"]
    assert [t.tool_name for t in await no_sql.load_tools()] == [
        "dashboard___open",
        "dashboard___snapshot",
    ]
