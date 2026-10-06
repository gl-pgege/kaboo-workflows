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

DEPENDS ON strands internals: ``Agent._middleware_registry`` and
``strands._middleware.stages.InvokeModelStage`` (strands 1.44 or later). strands'
own ``ContextInjector`` plugin uses the same seam.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace
from typing import Any, cast

from strands._middleware.stages import InvokeModelContext, InvokeModelStage
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AgentInitializedEvent
from strands.types.content import Messages

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from ._transcript import foreign_calls_to_notes

logger = logging.getLogger(__name__)


class ContextHook(HookProvider):
    """Rewrites the copy of an agent's conversation its model is sent."""

    def __init__(self) -> None:
        """Initialize the ContextHook.

        Pass it to the agent at construction (``Agent(hooks=[...])``): it
        installs its middleware when the agent finishes initializing, so a hook
        added to an existing agent never runs.
        """

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
        messages = foreign_calls_to_notes(original, tools)
        if messages is not original:
            logger.debug(
                "agent=<%s> | sent the model notes for calls to tools it does not have",
                context.agent.name,
            )
        return replace(context, messages=cast("Messages", messages))
