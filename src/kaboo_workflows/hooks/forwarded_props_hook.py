"""Expose the request's forwarded props to agents; optionally apply overrides.

``ForwardedPropsHook`` copies the AG-UI request's ``forwardedProps`` (bound by
the adapter via :func:`kaboo_workflows._context.set_forwarded_props`) into the
executing agent's state on every invocation, so agent- and hook-level code can
read the host's per-run side channel without importing the request context.

With ``apply_agent_config=True`` it also applies the run's agent configuration
from ``forwarded_props["agent_config"]`` (``agentConfig`` is accepted for
JS-host ergonomics):

- ``system_prompt`` / ``systemPrompt`` — replaces the agent's system prompt
  for this thread's clone.
- ``model_id`` / ``modelId`` — swaps the clone's model for a fresh instance of
  the same provider class with the new model id.

This is how a host serves many logical agent types from one runtime: it
projects the selected agent's prompt/model into ``forwarded_props`` per run,
and the override lands on the per-thread agent clone ag-ui-strands executes —
so concurrent runs of different agent types never interfere, and editing a
host-side agent definition takes effect on the next turn without a restart.

The model swap builds ``type(agent.model)(client_args=..., **config)`` rather
than mutating the shared config in place, because the blueprint's model
instance is shared across thread clones. Providers whose constructors require
non-config state (for example a custom boto session) keep their config but not
that state across a swap; id-only overrides are the supported contract.
"""

from __future__ import annotations

import logging
import sys
import warnings
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeInvocationEvent

from .._context import get_forwarded_props

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

logger = logging.getLogger(__name__)

_CONFIG_KEYS = ("agent_config", "agentConfig")


class ForwardedPropsHook(HookProvider):
    """Copy forwarded props into agent state; optionally apply agent overrides.

    Args:
        apply_agent_config: When ``True``, ``forwarded_props.agent_config``
            (``system_prompt``/``model_id``) is applied to the executing agent
            before each invocation. **Deprecated** — a run can now submit its
            whole config, which expresses the prompt and model along with the
            structure they could not. See ``create_agui_app(session_config_key=)``.
        state_key: Agent-state key the props are stored under.
    """

    def __init__(
        self,
        *,
        apply_agent_config: bool = False,
        state_key: str = "forwarded_props",
    ) -> None:
        if apply_agent_config:
            warnings.warn(
                "runtime.allow_invocation_overrides / forwardedProps.agent_config is "
                "deprecated and will be removed in 0.15.0. Submit the run's config "
                "instead: create_agui_app(config, session_config_key='workflow_config').",
                DeprecationWarning,
                stacklevel=2,
            )
        self._apply_agent_config = apply_agent_config
        self._state_key = state_key

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)

    def _on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        props = get_forwarded_props()
        event.agent.state.set(self._state_key, props)
        if not self._apply_agent_config:
            return
        config = next(
            (props[key] for key in _CONFIG_KEYS if isinstance(props.get(key), dict)),
            None,
        )
        if config is not None:
            self._apply(event.agent, config)

    def _apply(self, agent: Any, config: dict[str, Any]) -> None:
        system_prompt = config.get("system_prompt") or config.get("systemPrompt")
        if isinstance(system_prompt, str) and system_prompt.strip():
            agent.system_prompt = system_prompt

        model_id = config.get("model_id") or config.get("modelId")
        if not (isinstance(model_id, str) and model_id):
            return
        try:
            model_config = dict(agent.model.get_config())
        except Exception:  # pragma: no cover - provider without get_config
            logger.warning(
                "forwarded_props model override skipped: %s has no readable config",
                type(agent.model).__name__,
            )
            return
        if model_config.get("model_id") == model_id:
            return
        model_config["model_id"] = model_id
        kwargs: dict[str, Any] = {}
        client_args = getattr(agent.model, "client_args", None)
        if client_args is not None:
            kwargs["client_args"] = dict(client_args)
        try:
            agent.model = type(agent.model)(**kwargs, **model_config)
        except Exception:
            logger.exception(
                "forwarded_props model override to %r failed; keeping current model",
                model_id,
            )
