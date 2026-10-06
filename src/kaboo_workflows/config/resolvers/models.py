"""Resolve ModelDef -> strands model instance."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Literal

from strands.models import Model

from ...exceptions import ConfigurationError
from ...models import PROVIDERS, create_model
from ...utils import load_object

if TYPE_CHECKING:
    from ..schema import ModelDef, ThinkingDef

logger = logging.getLogger(__name__)

_MODEL_DEF_ATTR = "_kaboo_model_def"
_VARIANTS_ATTR = "_kaboo_thinking_variants"


def _thinking_settings(
    value: bool | ThinkingDef | None,
) -> dict[str, Any] | Literal[False] | None:
    if value is None:
        return None
    if value is False:
        return False
    if value is True:
        return {}
    return value.model_dump(exclude_none=True)


def effective_thinking(
    model_thinking: bool | ThinkingDef | None,
    agent_thinking: bool | ThinkingDef | None = None,
) -> dict[str, Any] | Literal[False] | None:
    """Return the thinking an agent runs with: its own setting laid over its model's.

    ``False`` means off, a mapping means adaptive thinking with those settings (empty
    for the provider's), and ``None`` means nothing is sent.
    """
    base = _thinking_settings(model_thinking)
    own = _thinking_settings(agent_thinking)
    if own is None:
        return base
    if own is False:
        return False
    return {**base, **own} if isinstance(base, dict) else own


def resolve_model(model_def: ModelDef, thinking: bool | ThinkingDef | None = None) -> Model:
    """Resolve a ModelDef to a strands model instance.

    Built-in providers (``"ollama"``, ``"bedrock"``, ``"openai"``,
    ``"gemini"``) are dispatched via :func:`~kaboo_workflows.models.create_model`.
    Any other ``provider`` value is treated as an import spec
    (``module.path:ClassName``) for a custom :class:`~strands.models.Model`
    subclass.

    Args:
        model_def: Parsed model definition from YAML.
        thinking: An agent's ``thinking:``, laid over the model's own.

    Returns:
        A strands-compatible model instance.

    Raises:
        ValueError: If the custom model class is not a Model subclass, or a
            thinking setting reaches a provider that does not take one.
        ImportError: If a required optional provider package is not installed.
    """
    settings = effective_thinking(model_def.thinking, thinking)
    if model_def.provider.lower() in {p.lower() for p in PROVIDERS}:
        model = create_model(
            model_def.provider, model_def.model_id, thinking=settings, **model_def.params
        )
    else:
        # Custom provider — load class from import spec
        model_cls = load_object(model_def.provider, target="model class")
        if not issubclass(model_cls, Model):
            raise ValueError(
                f"Custom model class '{model_def.provider}' must be a subclass of "
                "strands.models.Model."
            )
        if settings is not None:
            raise ValueError(
                f"thinking is supported on bedrock models only, not '{model_def.provider}'."
            )
        model = model_cls(model_id=model_def.model_id, **model_def.params)
    setattr(model, _MODEL_DEF_ATTR, model_def)
    setattr(model, _VARIANTS_ATTR, {})
    return model


def with_thinking(model: Model, thinking: bool | ThinkingDef) -> Model:
    """Return the instance of a named model that an agent with its own ``thinking:`` runs on.

    The model itself when the agent's setting changes nothing; otherwise a variant
    built from the same ModelDef and kept with the model, so every agent and every
    run with that setting shares one instance.

    Raises:
        ConfigurationError: If *model* was not resolved from a ModelDef.
    """
    model_def: ModelDef | None = getattr(model, _MODEL_DEF_ATTR, None)
    if model_def is None:
        raise ConfigurationError(
            f"thinking: needs a model resolved from a models: entry, not {type(model).__name__}."
        )
    settings = effective_thinking(model_def.thinking, thinking)
    if settings == effective_thinking(model_def.thinking):
        return model
    variants: dict[str, Model] = getattr(model, _VARIANTS_ATTR)
    key = json.dumps(settings, sort_keys=True)
    if key not in variants:
        variants[key] = resolve_model(model_def, thinking)
        logger.info("model=<%s>, thinking=<%s> | resolved model variant", model_def.model_id, key)
    return variants[key]
