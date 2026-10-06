"""thinking: a model's default and an agent's override, sent to Bedrock as request fields."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from kaboo_workflows.config.loaders.loaders import load, load_config, load_session
from kaboo_workflows.config.resolvers.config import resolve_infra
from kaboo_workflows.config.resolvers.models import (
    effective_thinking,
    resolve_model,
    with_thinking,
)
from kaboo_workflows.config.schema import ModelDef, ThinkingDef
from kaboo_workflows.exceptions import ConfigurationError, SchemaValidationError
from kaboo_workflows.models import create_model
from tests.factories import yaml_config
from tests.fakes import FakeModel

SONNET: dict[str, Any] = {
    "provider": "bedrock",
    "model_id": "m",
    "params": {"region_name": "us-east-1"},
    "thinking": {"effort": "high", "display": "summarized"},
}

CONFIG = """
models:
  sonnet:
    provider: bedrock
    model_id: m
    params: {region_name: us-east-1}
    thinking: {effort: high, display: summarized}
agents:
  lead: {model: sonnet}
  analyst: {model: sonnet, thinking: off}
  reviewer: {model: sonnet, thinking: false}
entry: lead
"""


def _fields(model: Any) -> dict[str, Any]:
    return model.get_config().get("additional_request_fields") or {}


def _bedrock(**kwargs: Any) -> Any:
    return create_model("bedrock", "m", region_name="us-east-1", **kwargs)


def _setting(value: Any) -> Any:
    return ThinkingDef(**value) if isinstance(value, dict) else value


# ── The request fields ──────────────────────────────────────────────────────


def test_off_disables_thinking():
    assert _fields(_bedrock(thinking=False)) == {"thinking": {"type": "disabled"}}


def test_on_sends_only_the_settings_that_are_set():
    assert _fields(_bedrock(thinking={"effort": "high", "display": "summarized"})) == {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "high"},
    }
    assert _fields(_bedrock(thinking=True)) == {"thinking": {"type": "adaptive"}}


def test_unset_sends_nothing():
    assert _fields(_bedrock()) == {}


def test_other_request_fields_are_kept_and_output_config_is_merged():
    model = _bedrock(
        thinking={"effort": "low"},
        additional_request_fields={"anthropic_beta": ["x"], "output_config": {"format": "json"}},
    )
    assert _fields(model) == {
        "anthropic_beta": ["x"],
        "output_config": {"format": "json", "effort": "low"},
        "thinking": {"type": "adaptive"},
    }


def test_thinking_set_twice_is_refused():
    with pytest.raises(ValueError, match="not both"):
        _bedrock(thinking=False, additional_request_fields={"thinking": {"type": "disabled"}})


def test_other_providers_refuse_thinking():
    with pytest.raises(ValueError, match="bedrock models only"):
        create_model("openai", "gpt-x", thinking=False)


# ── Merging an agent's setting over its model's ─────────────────────────────


@pytest.mark.parametrize(
    ("model", "agent", "expected"),
    [
        (None, None, None),
        (SONNET["thinking"], None, {"effort": "high", "display": "summarized"}),
        (SONNET["thinking"], False, False),
        (SONNET["thinking"], {"effort": "low"}, {"effort": "low", "display": "summarized"}),
        ({"effort": "high"}, True, {"effort": "high"}),
        (False, True, {}),
        (False, {"effort": "low"}, {"effort": "low"}),
        (None, False, False),
    ],
)
def test_an_agent_setting_is_laid_over_its_model(model, agent, expected):
    assert effective_thinking(_setting(model), _setting(agent)) == expected


def test_a_model_default_reaches_the_request():
    assert _fields(resolve_model(ModelDef(**SONNET)))["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }


# ── One instance per model and setting ──────────────────────────────────────


def test_an_agent_that_changes_nothing_shares_its_model():
    model = resolve_model(ModelDef(**SONNET))
    assert with_thinking(model, True) is model
    assert with_thinking(model, ThinkingDef(effort="high")) is model


def test_one_variant_per_setting_is_built_and_kept():
    model = resolve_model(ModelDef(**SONNET))
    off = with_thinking(model, False)
    assert off is not model
    assert with_thinking(model, False) is off
    assert _fields(off) == {"thinking": {"type": "disabled"}}
    assert _fields(with_thinking(model, ThinkingDef(effort="low"))) == {
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": "low"},
    }


def test_a_model_not_built_from_a_definition_takes_no_agent_thinking():
    with pytest.raises(ConfigurationError, match="models: entry"):
        with_thinking(FakeModel(), False)


def test_agents_with_the_same_setting_share_one_instance():
    agents = load(yaml_config(CONFIG)).agents
    lead, analyst, reviewer = (agents[name].model for name in ("lead", "analyst", "reviewer"))
    assert analyst is reviewer
    assert analyst is not lead
    assert _fields(analyst) == {"thinking": {"type": "disabled"}}
    assert _fields(lead)["thinking"]["display"] == "summarized"


def test_every_session_reuses_the_variant():
    config = load_config(yaml_config(CONFIG))
    infra = resolve_infra(config)
    first = load_session(config, infra).agents["analyst"].model
    assert load_session(config, infra).agents["analyst"].model is first


# ── Validation ──────────────────────────────────────────────────────────────


def test_yaml_off_and_false_both_turn_thinking_off():
    agents = load_config(yaml_config(CONFIG)).agents
    assert agents["analyst"].thinking is False
    assert agents["reviewer"].thinking is False


def test_an_unknown_setting_is_refused():
    with pytest.raises(SchemaValidationError, match="thinking"):
        load_config(yaml_config(CONFIG.replace("effort: high", "budget_tokens: 1024")))


def test_another_provider_refuses_a_model_default():
    with pytest.raises(ValidationError, match="bedrock models only"):
        ModelDef(provider="openai", model_id="gpt-x", thinking=False)


def test_a_model_default_and_raw_request_fields_are_refused_together():
    with pytest.raises(ValidationError, match="not both"):
        ModelDef(
            provider="bedrock",
            model_id="m",
            params={"additional_request_fields": {"thinking": {"type": "disabled"}}},
            thinking=False,
        )


def test_agent_thinking_needs_a_model():
    with pytest.raises(ConfigurationError, match="no model"):
        load_config(yaml_config("agents:\n  a: {thinking: false}\nentry: a\n"))


def test_agent_thinking_needs_a_bedrock_model():
    with pytest.raises(ConfigurationError, match="provider 'openai'"):
        load_config(
            yaml_config(
                """
                models:
                  gpt: {provider: openai, model_id: gpt-x}
                agents:
                  a: {model: gpt, thinking: false}
                entry: a
                """
            )
        )
