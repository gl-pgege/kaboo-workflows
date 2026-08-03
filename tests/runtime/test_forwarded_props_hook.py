"""ForwardedPropsHook: state copy + opt-in per-invocation agent overrides."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from kaboo_workflows._context import set_forwarded_props
from kaboo_workflows.hooks import ForwardedPropsHook


@pytest.fixture(autouse=True)
def _isolate() -> Iterator[None]:
    set_forwarded_props(None)
    yield
    set_forwarded_props(None)


class FakeState:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class FakeModel:
    def __init__(self, client_args: dict | None = None, **config: Any) -> None:
        self.client_args = client_args or {}
        self._config = config

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        state=FakeState(),
        system_prompt="original prompt",
        model=FakeModel(client_args={"api_key": "k"}, model_id="model-a"),
    )


def _event(agent: SimpleNamespace) -> Any:
    """Stub BeforeInvocationEvent (only ``.agent`` is read)."""
    return SimpleNamespace(agent=agent)


def test_copies_props_into_agent_state():
    set_forwarded_props({"runToken": "t", "agent_config": {"system_prompt": "new"}})
    agent = _agent()
    ForwardedPropsHook()._on_before_invocation(_event(agent))
    assert agent.state.data["forwarded_props"]["runToken"] == "t"


def test_overrides_off_by_default():
    set_forwarded_props({"agent_config": {"system_prompt": "new", "model_id": "model-b"}})
    agent = _agent()
    ForwardedPropsHook()._on_before_invocation(_event(agent))
    assert agent.system_prompt == "original prompt"
    assert agent.model.get_config()["model_id"] == "model-a"


def test_applies_system_prompt_and_model_when_enabled():
    set_forwarded_props({"agent_config": {"system_prompt": "new", "model_id": "model-b"}})
    agent = _agent()
    ForwardedPropsHook(apply_agent_config=True)._on_before_invocation(_event(agent))
    assert agent.system_prompt == "new"
    assert agent.model.get_config()["model_id"] == "model-b"
    # Fresh instance of the same class, carrying the client args over.
    assert type(agent.model) is FakeModel
    assert agent.model.client_args == {"api_key": "k"}


def test_accepts_camel_case_keys():
    set_forwarded_props({"agentConfig": {"systemPrompt": "camel", "modelId": "model-c"}})
    agent = _agent()
    ForwardedPropsHook(apply_agent_config=True)._on_before_invocation(_event(agent))
    assert agent.system_prompt == "camel"
    assert agent.model.get_config()["model_id"] == "model-c"


def test_same_model_id_keeps_instance():
    set_forwarded_props({"agent_config": {"model_id": "model-a"}})
    agent = _agent()
    original_model = agent.model
    ForwardedPropsHook(apply_agent_config=True)._on_before_invocation(_event(agent))
    assert agent.model is original_model


def test_blank_prompt_and_missing_config_are_ignored():
    set_forwarded_props({"agent_config": {"system_prompt": "   "}})
    agent = _agent()
    ForwardedPropsHook(apply_agent_config=True)._on_before_invocation(_event(agent))
    assert agent.system_prompt == "original prompt"

    set_forwarded_props({})
    ForwardedPropsHook(apply_agent_config=True)._on_before_invocation(_event(agent))
    assert agent.system_prompt == "original prompt"


def test_failed_model_swap_keeps_current_model():
    class ExplodingModel(FakeModel):
        def __init__(self, client_args: dict | None = None, **config: Any) -> None:
            if config.get("model_id") == "model-bad":
                raise RuntimeError("boom")
            super().__init__(client_args, **config)

    agent = _agent()
    agent.model = ExplodingModel(client_args={}, model_id="model-a")
    original_model = agent.model
    set_forwarded_props({"agent_config": {"model_id": "model-bad"}})
    ForwardedPropsHook(apply_agent_config=True)._on_before_invocation(_event(agent))
    assert agent.model is original_model
