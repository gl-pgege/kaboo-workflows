"""Default client timeout/retries for the openai provider."""

from __future__ import annotations

from typing import Any

from kaboo_workflows.models import (
    DEFAULT_OPENAI_MAX_RETRIES,
    DEFAULT_OPENAI_TIMEOUT_SECONDS,
    create_model,
)


def _client_args(model: Any) -> dict[str, Any]:
    return model.client_args


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
