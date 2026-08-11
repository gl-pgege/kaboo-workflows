"""Transport ``timeout`` option: scalar/dict httpx timeouts on streamable-http.

Long-running MCP tools (minutes of silence on the POST response stream) need
the read timeout raised above httpx's 5s default; ``transport_options.timeout``
is the YAML-reachable knob, honored both by the plain transport factory and by
the dedicated client the declarative ``auth:`` wiring builds.
"""

from __future__ import annotations

import httpx

from kaboo_workflows.auth import apply_auth_to_transport_options, build_auth
from kaboo_workflows.mcp.transports import _httpx_timeout


def test_scalar_timeout_applies_to_all_phases() -> None:
    timeout = _httpx_timeout(840)
    assert timeout.read == 840
    assert timeout.connect == 840
    assert timeout.write == 840
    assert timeout.pool == 840


def test_dict_timeout_sets_named_phases_and_defaults_the_rest() -> None:
    timeout = _httpx_timeout({"connect": 5, "read": 840})
    assert timeout.connect == 5
    assert timeout.read == 840
    assert timeout.write == 5.0
    assert timeout.pool == 5.0


def test_auth_wiring_folds_timeout_into_the_dedicated_client() -> None:
    auth = build_auth("static", {"token": "tok"})
    opts = apply_auth_to_transport_options(
        {"timeout": {"read": 840}, "terminate_on_close": False},
        auth,
        transport="streamable-http",
    )
    client = opts["http_client"]
    assert isinstance(client, httpx.AsyncClient)
    assert client.timeout.read == 840
    assert "timeout" not in opts
    assert opts["terminate_on_close"] is False


def test_auth_wiring_without_timeout_keeps_httpx_defaults() -> None:
    auth = build_auth("static", {"token": "tok"})
    opts = apply_auth_to_transport_options({}, auth, transport="streamable-http")
    assert opts["http_client"].timeout == httpx.Timeout(5.0)
