"""Reshape what an agent sends its model, never what the agent keeps.

:class:`ContextHook` installs itself on strands' model-input middleware
(``InvokeModelStage``), which hands it a deep copy of ``agent.messages`` before
every model call. Whatever it changes reaches the model only: the agent's own
messages, the chat the client renders and stores (``MESSAGES_SNAPSHOT``), the
transcript captured into ``kaboo_history`` and anything a session manager
persists all keep the original conversation.

On every call it turns a ``toolUse`` for a tool the agent does not have into a
text note. Such calls reach an agent through a transcript it did not write: the
chat, when the workflow changed between turns, or a ``history.group`` shared
with another agent. A model shown a call it cannot make tends to make it anyway.

With a ``clear_trigger`` it also clears old tool results once the estimated
input grows past it (``context.clear_tool_results`` on an agent).

When a :class:`~kaboo_workflows.hooks.HistoryHook` seeded the agent with memory
and the model is a ``BedrockModel`` that does not manage its own cache points
(no ``cache_config``), it ends the last loaded message with a ``cachePoint``, so
every later model turn of the call reads the memory from Bedrock's prompt cache.

DEPENDS ON strands internals: ``Agent._middleware_registry`` and
``strands._middleware.stages.InvokeModelStage`` (strands 1.44 or later). strands'
own ``ContextInjector`` plugin uses the same seam.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Collection
from dataclasses import replace
from typing import Any, cast

from strands._middleware.stages import InvokeModelContext, InvokeModelStage
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AgentInitializedEvent
from strands.models import Model
from strands.models.bedrock import BedrockModel
from strands.types.content import Messages

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from ..tools.wrappers import delegate_tool_names
from ._transcript import (
    DEFAULT_MIN_CHARS,
    SEEDED_MESSAGES,
    clear_tool_results,
    estimate_tokens,
    foreign_calls_to_notes,
)

logger = logging.getLogger(__name__)


class ContextHook(HookProvider):
    """Rewrites the copy of an agent's conversation its model is sent."""

    def __init__(
        self,
        *,
        clear_trigger: int | None = None,
        keep_last: int = 3,
        min_chars: int = DEFAULT_MIN_CHARS,
        keep_tools: Collection[str] = (),
    ) -> None:
        """Initialize the ContextHook.

        Pass it to the agent at construction (``Agent(hooks=[...])``): it
        installs its middleware when the agent finishes initializing, so a hook
        added to an existing agent never runs.

        Args:
            clear_trigger: Estimated input tokens (system prompt plus messages,
                four characters a token) past which old tool results are
                cleared before a model call. ``None`` never clears.
            keep_last: How many of the most recent turns of tool results always
                stay; a turn is every result of one model response.
            min_chars: Tool results shorter than this always stay.
            keep_tools: Tools whose results always stay. A delegate's always do.
        """
        self._clear_trigger = clear_trigger
        self._keep_last = keep_last
        self._min_chars = min_chars
        self._keep_tools = frozenset(keep_tools)

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Install the model-input middleware once the agent is initialized."""
        registry.add_callback(AgentInitializedEvent, self._on_initialized)

    def _on_initialized(self, event: AgentInitializedEvent) -> None:
        event.agent._middleware_registry.add_middleware(InvokeModelStage.Input, self._model_input)

    def _model_input(self, context: InvokeModelContext) -> InvokeModelContext:
        registry = context.agent.tool_registry
        tools = {*registry.registry, *registry.dynamic_tools}
        original = cast("list[dict[str, Any]]", context.messages)
        seeded = context.invocation_state.get(SEEDED_MESSAGES)
        if (
            isinstance(seeded, int)
            and 0 < seeded <= len(original)
            and _manual_bedrock_cache(context.agent.model)
        ):
            original[seeded - 1]["content"].append({"cachePoint": {"type": "default"}})
        messages = foreign_calls_to_notes(original, tools)
        if messages is not original:
            logger.debug(
                "agent=<%s> | sent the model notes for calls to tools it does not have",
                context.agent.name,
            )
        if (
            self._clear_trigger is not None
            and estimate_tokens(messages, context.system_prompt) > self._clear_trigger
        ):
            messages = clear_tool_results(
                messages,
                keep_last=self._keep_last,
                min_chars=self._min_chars,
                kept=self._keep_tools | delegate_tool_names(context.agent),
                tools=tools,
            )
        return replace(context, messages=cast("Messages", messages))


def _manual_bedrock_cache(model: Model) -> bool:
    """Whether a cache point placed by hand reaches Bedrock.

    A ``cache_config`` makes ``BedrockModel`` strip every cache point it is
    given and place its own.
    """
    return isinstance(model, BedrockModel) and not model.config.get("cache_config")
