"""Outbound auth strategies: what the caller configures is what goes on the wire.

Deployment differences (a gateway that overwrites ``Authorization`` so the token
has to ride another header) must be reachable from YAML, because the alternative
is a library release per deployment. Token exchange is deliberately not one of
these strategies: it belongs to whatever sits between the agent and the service.
"""

from __future__ import annotations

import httpx
import pytest

from kaboo_workflows._context import (
    Principal,
    set_activity_context,
    set_auth_context,
    set_forwarded_props,
)
from kaboo_workflows.auth import build_auth


def _request() -> httpx.Request:
    return httpx.Request("POST", "http://service.internal/mcp")


def _apply(auth: httpx.Auth) -> httpx.Request:
    flow = auth.sync_auth_flow(_request())
    request = next(flow)
    flow.close()
    return request


@pytest.fixture
def user_principal() -> None:
    set_auth_context(Principal(token="user-jwt", headers={}))


def test_relay_header_and_scheme_come_from_config(user_principal: None) -> None:
    # A managed AgentCore Gateway replaces Authorization outbound, so the run
    # token has to be movable to a declared header without a code change.
    auth = build_auth("relay", {"header": "x-kaboo-run-token", "scheme": ""})

    request = _apply(auth)

    assert request.headers["x-kaboo-run-token"] == "user-jwt"
    assert "authorization" not in request.headers


def test_relay_sends_the_token_in_every_configured_header(
    user_principal: None,
) -> None:
    # A managed gateway authenticates the caller on Authorization and then
    # replaces it, so reaching the service behind it needs both headers.
    auth = build_auth("relay", {"header": ["Authorization", "x-kaboo-run-token"]})

    request = _apply(auth)

    assert request.headers["authorization"] == "Bearer user-jwt"
    assert request.headers["x-kaboo-run-token"] == "Bearer user-jwt"


def test_relay_stamps_run_context_headers_from_config(user_principal: None) -> None:
    # Services date scripts and group a run's edits by these, and a gateway
    # passes only the headers it is told to, so which ones go is config.
    set_activity_context(thread_id="thread-1", run_id="run-1")
    set_forwarded_props({"timezone": "America/Toronto"})
    try:
        auth = build_auth(
            "relay",
            {
                "context_headers": {
                    "x-kaboo-run-id": "run_id",
                    "x-kaboo-timezone": "forwarded_props.timezone",
                    "x-kaboo-locale": "forwarded_props.locale",
                }
            },
        )

        request = _apply(auth)
    finally:
        set_activity_context(thread_id=None)
        set_forwarded_props(None)

    assert request.headers["authorization"] == "Bearer user-jwt"
    assert request.headers["x-kaboo-run-id"] == "run-1"
    assert request.headers["x-kaboo-timezone"] == "America/Toronto"
    assert "x-kaboo-locale" not in request.headers


def test_an_unknown_context_header_source_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown context header source 'run'"):
        build_auth("relay", {"context_headers": {"x-kaboo-run-id": "run"}})


def test_an_empty_header_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty header name"):
        build_auth("relay", {"header": []})


def test_obo_is_gone_and_says_so() -> None:
    # Removed in 0.20.0: on-behalf-of belongs to the gateway, which can exchange
    # against an authorization server that knows what a project is. Keeping it
    # here would make the runtime opinionated about identity.
    with pytest.raises(ValueError, match="Unknown MCP auth strategy 'obo'"):
        build_auth("obo", {"provider": "spreadsheet-api"})
