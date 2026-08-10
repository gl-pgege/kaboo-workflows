"""The endpoint's contract with the session serving a request.

Two properties, both about lifetime rather than behaviour: a run that owns its
resources releases them when its stream ends, and a run whose submitted config is
bad is answered with an error instead of a 500.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from ag_ui.core import RunAgentInput, UserMessage
from fastapi import FastAPI

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters.agui import (
    AguiSession,
    _add_kaboo_endpoint,
    _build_session,
    _make_session_resolver,
)
from kaboo_workflows.config import parse_config_sources, resolve_infra, validate_raw_config
from kaboo_workflows.exceptions import ConfigurationError
from kaboo_workflows.mcp.lifecycle import MCPLifecycle
from tests.fakes import FakeMCPClient

BASE_YAML = """
agents:
  assistant:
    model:
      provider: tests.fakes.scripted_model:ScriptedModel
      model_id: scripted
      params:
        script: []
        final_text: "done"
entry: assistant
"""


class _StubRequest:
    """Only what the endpoint reads from a request when no auth verifier is set."""

    headers: dict[str, str] = {}


def _input(**props: Any) -> RunAgentInput:
    return RunAgentInput(
        thread_id="t1",
        run_id="r1",
        state={},
        messages=[UserMessage(id="m1", role="user", content="go")],
        tools=[],
        context=[],
        forwarded_props=props,
    )


def _endpoint(resolve: Any) -> Any:
    app = FastAPI()
    _add_kaboo_endpoint(app, resolve, ActivityRegistry(), "/invocations")
    return cast("Any", app.routes[-1]).endpoint


async def _drain(response: Any) -> str:
    return "".join([chunk async for chunk in response.body_iterator])


def _session(*, transient: bool, clients: MCPLifecycle | None = None) -> AguiSession:
    app_config = validate_raw_config(parse_config_sources(BASE_YAML))
    return _build_session(
        app_config,
        resolve_infra(app_config),
        mcp_clients=clients,
        transient=transient,
    )


async def test_a_run_releases_the_mcp_clients_it_owns_when_its_stream_ends():
    client = FakeMCPClient()
    clients = MCPLifecycle()
    clients.add_client("gateway", client)  # ty: ignore[invalid-argument-type]
    clients.start(pin_clients=False)
    session = _session(transient=True, clients=clients)

    response = await _endpoint(lambda _i: session)(_input(), _StubRequest())
    await _drain(response)

    # This is what makes "the client session is not running" impossible rather
    # than patched: the session cannot outlive the run that opened it.
    assert client.calls == ["stop"]


async def test_a_process_wide_session_is_left_open_between_runs():
    client = FakeMCPClient()
    clients = MCPLifecycle()
    clients.add_client("gateway", client)  # ty: ignore[invalid-argument-type]
    clients.start(pin_clients=False)
    session = _session(transient=False, clients=clients)

    response = await _endpoint(lambda _i: session)(_input(), _StubRequest())
    await _drain(response)

    assert client.calls == []


async def test_a_bad_submitted_config_answers_the_run_with_an_error():
    infra = resolve_infra(validate_raw_config(parse_config_sources(BASE_YAML)))
    resolve = _make_session_resolver(
        parse_config_sources(BASE_YAML), infra, session_config_key="workflow_config"
    )

    response = await _endpoint(resolve)(
        _input(workflow_config="agents:\n  a:\n    mcp: [missing]\nentry: a"),
        _StubRequest(),
    )
    body = await _drain(response)

    assert "INVALID_CONFIG" in body
    # The message names the unresolvable reference, so the author can fix it.
    assert "missing" in body


async def test_each_run_gets_its_own_client_sessions():
    infra = resolve_infra(validate_raw_config(parse_config_sources(BASE_YAML)))
    resolve = _make_session_resolver(
        parse_config_sources(BASE_YAML), infra, session_config_key="workflow_config"
    )

    first = resolve(_input())
    second = resolve(_input())

    assert first is not second
    assert first.mcp_clients is not second.mcp_clients
    assert first.transient and second.transient


async def test_a_submitted_client_url_off_the_allowlist_is_refused():
    base_raw = parse_config_sources(BASE_YAML)
    infra = resolve_infra(validate_raw_config(base_raw))
    resolve = _make_session_resolver(
        base_raw,
        infra,
        session_config_key="workflow_config",
        allowed_mcp_hosts=["gateway.internal"],
    )

    with pytest.raises(ConfigurationError):
        resolve(
            _input(
                workflow_config=(
                    "mcp_clients:\n  x:\n    url: http://elsewhere.example/mcp\n"
                    "agents:\n  a: {}\nentry: a"
                )
            )
        )
