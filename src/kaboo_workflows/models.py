"""LLM model factory."""

from __future__ import annotations

from typing import Any

from strands.models import Model

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


def create_model(provider: str, model_id: str, **params: Any) -> Model:
    """Dispatch to the appropriate model factory by provider name.

    Args:
        provider: ``"ollama"`` or ``"bedrock"`` or ``"openai"`` or ``"gemini"``.
        model_id: Model identifier.
        **params: Provider-specific keyword arguments.

    Returns:
        Strands model instance.

    Raises:
        ValueError: If the provider is unknown.
        ImportError: If a required optional provider package is not installed.
    """
    match provider.lower():
        case "bedrock":
            from strands.models.bedrock import BedrockModel

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
