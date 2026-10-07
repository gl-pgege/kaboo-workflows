"""Client settings for model providers: openai timeout/retries, bedrock client_config."""

from __future__ import annotations

from typing import Any

import pytest
from strands.models import CacheConfig

from kaboo_workflows.config.resolvers.models import resolve_model
from kaboo_workflows.models import (
    DEFAULT_OPENAI_MAX_RETRIES,
    DEFAULT_OPENAI_TIMEOUT_SECONDS,
    create_model,
)
from tests.factories import model_def


def _client_args(model: Any) -> dict[str, Any]:
    return model.client_args


def _boto_config(model: Any) -> Any:
    return model.client.meta.config


def test_openai_gets_default_timeout_and_retries():
    args = _client_args(create_model("openai", "gpt-x", client_args={"api_key": "k"}))
    assert args["timeout"] == DEFAULT_OPENAI_TIMEOUT_SECONDS
    assert args["max_retries"] == DEFAULT_OPENAI_MAX_RETRIES


def test_openai_defaults_apply_without_client_args():
    args = _client_args(create_model("openai", "gpt-x"))
    assert args["timeout"] == DEFAULT_OPENAI_TIMEOUT_SECONDS


def test_openai_user_values_win():
    args = _client_args(
        create_model(
            "openai", "gpt-x", client_args={"api_key": "k", "timeout": 30, "max_retries": 5}
        )
    )
    assert args["timeout"] == 30
    assert args["max_retries"] == 5


def test_bedrock_client_config_reaches_the_boto_client():
    model = resolve_model(
        model_def(
            provider="bedrock",
            model_id="anthropic.claude",
            params={
                "region_name": "us-east-1",
                "client_config": {
                    "read_timeout": 840,
                    "connect_timeout": 5,
                    "retries": {"max_attempts": 3, "mode": "adaptive"},
                },
            },
        )
    )

    config = _boto_config(model)
    assert config.read_timeout == 840
    assert config.connect_timeout == 5
    assert config.retries["mode"] == "adaptive"
    assert config.retries["total_max_attempts"] == 4


def test_bedrock_client_config_without_read_timeout_keeps_the_strands_default():
    with_retries_only = create_model(
        "bedrock",
        "anthropic.claude",
        region_name="us-east-1",
        client_config={"retries": {"mode": "standard"}},
    )
    without_client_config = create_model("bedrock", "anthropic.claude", region_name="us-east-1")

    assert (
        _boto_config(with_retries_only).read_timeout
        == _boto_config(without_client_config).read_timeout
    )
    assert _boto_config(with_retries_only).retries["mode"] == "standard"


def test_a_bedrock_cache_config_mapping_becomes_the_strands_setting():
    model = resolve_model(
        model_def(
            provider="bedrock",
            model_id="anthropic.claude",
            params={
                "region_name": "us-east-1",
                "cache_prompt": "default",
                "cache_tools": "default",
                "cache_config": {"strategy": "auto", "ttl": "1h"},
            },
        )
    )

    assert model.get_config()["cache_config"] == CacheConfig(strategy="auto", ttl="1h")


def test_a_bedrock_cache_config_with_an_unknown_key_fails_at_startup():
    with pytest.raises(ValueError, match="strategy and ttl"):
        create_model(
            "bedrock", "anthropic.claude", region_name="us-east-1", cache_config={"mode": "auto"}
        )
