"""LLM model factory."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from strands.models import Model

if TYPE_CHECKING:
    from botocore.config import Config as BotocoreConfig

PROVIDERS = ("bedrock", "ollama", "openai", "gemini")

# Defaults injected into the OpenAI-compatible client unless the config sets
# its own values. The SDK's default is effectively unbounded for streaming
# responses: a stalled connection (proxy, upstream router) would otherwise hang
# a run forever with no error. A finite timeout with retries turns that
# failure mode into a visible, recoverable one.
DEFAULT_OPENAI_TIMEOUT_SECONDS = 180.0
DEFAULT_OPENAI_MAX_RETRIES = 2


def _with_openai_client_defaults(params: dict[str, Any]) -> dict[str, Any]:
    """Return ``params`` with default ``client_args`` timeout/retries applied.

    User-supplied values always win; only absent keys are filled.
    """
    client_args = dict(params.get("client_args") or {})
    client_args.setdefault("timeout", DEFAULT_OPENAI_TIMEOUT_SECONDS)
    client_args.setdefault("max_retries", DEFAULT_OPENAI_MAX_RETRIES)
    return {**params, "client_args": client_args}


def _bedrock_client_config(client_config: dict[str, Any]) -> BotocoreConfig:
    """Return the botocore ``Config`` a Bedrock model's client is created with.

    strands only applies its own read timeout when it builds the ``Config``
    itself, so it is filled in here; naming just ``retries`` would otherwise
    shorten the timeout to botocore's default.
    """
    from botocore.config import Config
    from strands.models.bedrock import DEFAULT_READ_TIMEOUT

    return Config(**{"read_timeout": DEFAULT_READ_TIMEOUT, **client_config})


def _bedrock_thinking_fields(thinking: bool | Mapping[str, Any]) -> dict[str, Any]:
    """Return the ``additional_request_fields`` that carry a thinking setting to Bedrock."""
    if thinking is False:
        return {"thinking": {"type": "disabled"}}
    settings = {} if thinking is True else dict(thinking)
    block: dict[str, Any] = {"type": "adaptive"}
    if settings.get("display"):
        block["display"] = settings["display"]
    fields: dict[str, Any] = {"thinking": block}
    if settings.get("effort"):
        fields["output_config"] = {"effort": settings["effort"]}
    return fields


def _with_bedrock_thinking(
    params: dict[str, Any], thinking: bool | Mapping[str, Any]
) -> dict[str, Any]:
    """Return ``params`` with the thinking setting merged into ``additional_request_fields``."""
    extra = dict(params.get("additional_request_fields") or {})
    if "thinking" in extra:
        raise ValueError(
            "A bedrock model takes thinking or additional_request_fields.thinking, not both."
        )
    fields = _bedrock_thinking_fields(thinking)
    if "output_config" in fields:
        fields["output_config"] = {**(extra.get("output_config") or {}), **fields["output_config"]}
    return {**params, "additional_request_fields": {**extra, **fields}}


def create_model(
    provider: str,
    model_id: str,
    *,
    thinking: bool | Mapping[str, Any] | None = None,
    **params: Any,
) -> Model:
    """Dispatch to the appropriate model factory by provider name.

    Args:
        provider: ``"ollama"`` or ``"bedrock"`` or ``"openai"`` or ``"gemini"``.
        model_id: Model identifier.
        thinking: ``False`` to turn thinking off, ``True`` or a mapping of
            ``effort`` and ``display`` to turn adaptive thinking on, ``None`` to
            send nothing. Bedrock only: it becomes the model's
            ``additional_request_fields``.
        **params: Provider-specific keyword arguments. For ``"bedrock"``, a
            ``client_config`` mapping (``read_timeout``, ``connect_timeout``,
            ``retries``) becomes the model's ``boto_client_config``.

    Returns:
        Strands model instance.

    Raises:
        ValueError: If the provider is unknown, a non-Bedrock model is given a
            ``thinking`` setting, or a Bedrock model is given both
            ``client_config`` and ``boto_client_config``, or both ``thinking``
            and ``additional_request_fields.thinking``.
        ImportError: If a required optional provider package is not installed.
    """
    if thinking is not None and provider.lower() != "bedrock":
        raise ValueError(f"thinking is supported on bedrock models only, not '{provider}'.")
    match provider.lower():
        case "bedrock":
            from strands.models.bedrock import BedrockModel

            client_config = params.pop("client_config", None)
            if client_config is not None:
                if "boto_client_config" in params:
                    raise ValueError(
                        "A bedrock model takes client_config or boto_client_config, not both."
                    )
                params["boto_client_config"] = _bedrock_client_config(client_config)
            if thinking is not None:
                params = _with_bedrock_thinking(params, thinking)
            return BedrockModel(model_id=model_id, **params)

        case "ollama":
            try:
                from strands.models.ollama import OllamaModel
            except ImportError:
                raise ImportError(
                    "The 'ollama' provider requires the ollama extra:\n"
                    "  pip install kaboo-workflows[ollama]\n"
                    "Or install directly: pip install strands-agents[ollama]"
                ) from None
            return OllamaModel(model_id=model_id, **params)

        case "openai":
            try:
                from .models_openai import KabooOpenAIModel
            except ImportError:
                raise ImportError(
                    "The 'openai' provider requires the openai extra:\n"
                    "  pip install kaboo-workflows[openai]\n"
                    "Or install directly: pip install strands-agents[openai]"
                ) from None
            return KabooOpenAIModel(model_id=model_id, **_with_openai_client_defaults(params))

        case "gemini":
            try:
                from strands.models.gemini import GeminiModel
            except ImportError:
                raise ImportError(
                    "The 'gemini' provider requires the gemini extra:\n"
                    "  pip install kaboo-workflows[gemini]\n"
                    "Or install directly: pip install strands-agents[gemini]"
                ) from None
            return GeminiModel(model_id=model_id, **params)

        case _:
            raise ValueError(
                f"Unknown model provider '{provider}'.\nAvailable: {', '.join(PROVIDERS)}."
            )
