"""Outbound auth strategies: what the caller configures is what goes on the wire.

Provider differences (an Entra ID tenant wanting ``requested_token_use``, a
gateway that overwrites ``Authorization`` so the token has to ride another
header) must be reachable from YAML, because the alternative is a library
release per identity provider.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kaboo_workflows._context import Principal, set_auth_context
from kaboo_workflows.auth import OBOTokenAuth, build_auth


class FakeIdentity:
    """Stands in for the bedrock-agentcore client, recording both exchanges."""

    def __init__(self) -> None:
        self.jwt_calls: list[dict[str, Any]] = []
        self.token_calls: list[dict[str, Any]] = []

    def get_workload_access_token_for_jwt(self, **kwargs: Any) -> dict[str, str]:
        self.jwt_calls.append(kwargs)
        return {"workloadAccessToken": "workload-token"}

    def get_resource_oauth2_token(self, **kwargs: Any) -> dict[str, Any]:
        self.token_calls.append(kwargs)
        return {"accessToken": "downstream-token", "expiresIn": 3600}


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


def test_obo_exchanges_the_user_token_then_the_resource_token(
    user_principal: None,
) -> None:
    identity = FakeIdentity()
    auth = OBOTokenAuth(
        provider="spreadsheet-api",
        region="ca-central-1",
        workload_name="kaboo-agent",
        scopes=["api://kaboo/.default"],
    )
    auth._client = identity

    request = _apply(auth)

    assert identity.jwt_calls == [
        {"workloadName": "kaboo-agent", "userToken": "user-jwt"}
    ]
    assert identity.token_calls[0]["workloadIdentityToken"] == "workload-token"
    assert identity.token_calls[0]["scopes"] == ["api://kaboo/.default"]
    assert request.headers["Authorization"] == "Bearer downstream-token"


def test_obo_forwards_custom_parameters(user_principal: None) -> None:
    identity = FakeIdentity()
    auth = OBOTokenAuth(
        provider="entra",
        workload_name="kaboo-agent",
        custom_parameters={"requested_token_use": "on_behalf_of"},
    )
    auth._client = identity

    _apply(auth)

    assert identity.token_calls[0]["customParameters"] == {
        "requested_token_use": "on_behalf_of"
    }


def test_obo_sends_scopes_even_when_none_are_configured(user_principal: None) -> None:
    # The API declares scopes required, so omitting the field fails the call
    # rather than defaulting it.
    identity = FakeIdentity()
    auth = OBOTokenAuth(provider="spreadsheet-api", workload_name="kaboo-agent")
    auth._client = identity

    _apply(auth)

    assert identity.token_calls[0]["scopes"] == []


def test_obo_uses_an_inbound_workload_token_without_a_workload_name() -> None:
    set_auth_context(Principal(token=None, headers={"WorkloadAccessToken": "inbound"}))
    identity = FakeIdentity()
    auth = OBOTokenAuth(provider="spreadsheet-api")
    auth._client = identity

    _apply(auth)

    assert identity.jwt_calls == []
    assert identity.token_calls[0]["workloadIdentityToken"] == "inbound"


def test_obo_caches_the_downstream_token_per_workload_token(
    user_principal: None,
) -> None:
    identity = FakeIdentity()
    auth = OBOTokenAuth(provider="spreadsheet-api", workload_name="kaboo-agent")
    auth._client = identity

    _apply(auth)
    _apply(auth)

    assert len(identity.token_calls) == 1


def test_relay_header_and_scheme_come_from_config(user_principal: None) -> None:
    # A managed AgentCore Gateway replaces Authorization outbound, so the run
    # token has to be movable to a declared header without a code change.
    auth = build_auth("relay", {"header": "x-kaboo-run-token", "scheme": ""})

    request = _apply(auth)

    assert request.headers["x-kaboo-run-token"] == "user-jwt"
    assert "authorization" not in request.headers
