"""Reshape what an agent sends its model, never what the agent keeps.

:class:`ContextHook` installs itself on strands' model-input middleware
(``InvokeModelStage``), which hands it a deep copy of ``agent.messages`` before
every model call. Whatever it changes reaches the model only: the agent's own
messages, the chat the client renders and stores (``MESSAGES_SNAPSHOT``), the
transcript captured into ``kaboo_sessions`` and anything a session manager
persists all keep the original conversation.

On every call it joins neighbouring messages that share a role. The chat stores
a response that called several tools at once as one message per call, and a turn
the model never answered leaves two user messages in a row; replayed as they
are, Bedrock rejects both.

On every call it also turns a ``toolUse`` for a tool the agent does not have into a
text note. Such calls reach an agent through a transcript it did not write: the
chat, when the workflow changed between turns, or a ``history.group`` shared
with another agent. A model shown a call it cannot make tends to make it anyway.
Only what the agent inherited is rewritten: the messages it held at the first
model call of the invocation. A call it makes itself, even to a tool name it got
wrong, reaches its model as the call and its error.

With a ``clear_trigger`` it also clears old tool results once the estimated
input grows past it (``context.clear_tool_results`` on an agent).

When a :class:`~kaboo_workflows.hooks.HistoryHook` seeded the agent with memory
and the model is a ``BedrockModel`` that does not manage its own cache points
(no ``cache_config``), it ends the last loaded message with a ``cachePoint``, so
every later model turn of the call reads the memory from Bedrock's prompt cache.
If rewriting leaves that cache point opening a message, it moves to the end of
the message before.

Uses strands' public middleware API (``strands.middleware.InvokeModelStage`` and
``Agent.add_middleware``, strands 1.62 or later). On older strands the same
stages were private (``strands._middleware``, 1.44 to 1.61) and are reached there.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Collection
from dataclasses import replace
from typing import Any, cast

from strands import Agent
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AgentInitializedEvent
from strands.models import Model
from strands.models.bedrock import BedrockModel
from strands.types.content import Messages

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

try:
    from strands.middleware import InvokeModelContext, InvokeModelStage
except ImportError:  # strands 1.44 to 1.61 kept the stages private
    from strands._middleware.stages import (  # ty: ignore[unresolved-import]
        InvokeModelContext,
        InvokeModelStage,
    )

from .._context import get_run_context
from ..tools.wrappers import delegate_tool_names
from ._run_context import context_for, format_context
from ._transcript import (
    DEFAULT_MIN_CHARS,
    INHERITED_MESSAGES,
    SEEDED_MESSAGES,
    clear_tool_results,
    estimate_tokens,
    foreign_calls_to_notes,
    join_turns,
    settle_cache_points,
)
from .state_effects import workspace_view

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
        client_tools: Collection[str] = (),
        workspace: Collection[str] = (),
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
            client_tools: The browser tools the agent may call; context items
                that belong to another browser tool stay out of its calls.
            workspace: JSON Pointers into the shared state the agent sees at
                every model call (see
                :func:`~kaboo_workflows.hooks.state_effects.workspace_view`).
        """
        self._clear_trigger = clear_trigger
        self._keep_last = keep_last
        self._min_chars = min_chars
        self._keep_tools = frozenset(keep_tools)
        self._client_tools = frozenset(client_tools)
        self._workspace = tuple(workspace)

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Install the model-input middleware once the agent is initialized."""
        registry.add_callback(AgentInitializedEvent, self._on_initialized)

    def _on_initialized(self, event: AgentInitializedEvent) -> None:
        add = getattr(event.agent, "add_middleware", None)
        if callable(add):
            add(InvokeModelStage.Input, self._model_input)
        else:
            event.agent._middleware_registry.add_middleware(
                InvokeModelStage.Input, self._model_input
            )

    def _model_input(self, context: InvokeModelContext) -> InvokeModelContext:
        registry = context.agent.tool_registry
        tools = {*registry.registry, *registry.dynamic_tools}
        original = cast("list[dict[str, Any]]", context.messages)
        inherited = context.invocation_state.setdefault(INHERITED_MESSAGES, len(original))
        seeded = context.invocation_state.get(SEEDED_MESSAGES)
        if (
            isinstance(seeded, int)
            and 0 < seeded <= len(original)
            and _manual_bedrock_cache(context.agent.model)
        ):
            original[seeded - 1]["content"].append({"cachePoint": {"type": "default"}})
        noted = foreign_calls_to_notes(original, tools, inherited=inherited)
        if noted is not original:
            logger.debug(
                "agent=<%s> | sent the model notes for calls to tools it does not have",
                context.agent.name,
            )
        messages = join_turns(noted)
        if (
            self._clear_trigger is not None
            and estimate_tokens(messages, context.system_prompt) > self._clear_trigger
        ):
            messages = clear_tool_results(
                messages,
                keep_last=self._keep_last,
                min_chars=self._min_chars,
                kept=self._keep_tools | delegate_tool_names(cast("Agent", context.agent)),
                tools=tools,
            )
        block = format_context(context_for(get_run_context(), self._client_tools))
        view = workspace_view(self._workspace) if self._workspace else None
        for text in (view, block):
            if text:
                messages = with_context(messages, text)
        return replace(context, messages=cast("Messages", settle_cache_points(messages)))


def _manual_bedrock_cache(model: Model) -> bool:
    """Whether a cache point placed by hand reaches Bedrock.

    A ``cache_config`` makes ``BedrockModel`` strip every cache point it is
    given and place its own.
    """
    return isinstance(model, BedrockModel) and not model.config.get("cache_config")


def with_context(messages: list[dict[str, Any]], block: str) -> list[dict[str, Any]]:
    """Open the latest human message with the run's context, in a copy.

    The latest one, so a context that changes between runs leaves the earlier
    conversation as it was, and a prompt cache over it still applies.
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        content = message.get("content") or []
        if message.get("role") == "user" and any("text" in block_ for block_ in content):
            copied = list(messages)
            copied[index] = {**message, "content": [{"text": block}, *content]}
            return copied
    return [{"role": "user", "content": [{"text": block}]}, *messages]
