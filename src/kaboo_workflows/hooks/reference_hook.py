"""Per-agent reference manifest injection hook.

Injects a lightweight text **manifest** of the run's client-supplied references
(file attachments + custom entities) into an in-scope agent's system prompt on
every invocation, so any agent in a pipeline — not just the entry agent — knows
*what exists* and can resolve items on demand via a tool.

This mirrors :class:`~kaboo_workflows.hooks.history_hook.HistoryHook`: one
instance is attached per agent, it reads the request-scoped
:data:`~kaboo_workflows._context._current_references` contextvar on
``BeforeInvocationEvent``, and it is a no-op while an interrupt is active.

For ``inline`` agents the hook additionally prepends resolved media as strands
``ContentBlock``s so a vision/doc-capable model literally sees the file (see
:func:`~kaboo_workflows.tools.references.resolve_inline_blocks`).
"""

from __future__ import annotations

import logging
import sys
from typing import Any, cast

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeInvocationEvent, BeforeModelCallEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from .._context import (
    Reference,
    get_inline_requests,
    get_references,
    get_run_id,
    get_thread_id,
)

logger = logging.getLogger(__name__)

_BASE_PROMPT_ATTR = "_kaboo_base_system_prompt"
_INLINE_DONE_ATTR = "_kaboo_inline_injected"
_FETCHED_ATTR = "_kaboo_fetched_injected"


def build_manifest(references: list[Reference], *, tool_enabled: bool) -> str:
    """Render the reference manifest injected into an agent's system prompt.

    Lists each reference's ``kind``, ``name`` and ``id`` (the id the model
    passes to a resolver tool). When ``tool_enabled`` the note points file
    attachments at the built-in ``fetch_attachment`` tool; custom object kinds
    are always resolved by the app's own tool.
    """
    lines = [
        "## Referenced items",
        "The user attached or referenced the items below. They are pointers — "
        "fetch or query an item by its id when you need its contents.",
    ]
    for ref in references:
        detail = f"id={ref.id}"
        if ref.mime_type:
            detail += f", type={ref.mime_type}"
        lines.append(f"- [{ref.kind}] {ref.name} ({detail}) — {ref.transport}")
    if tool_enabled:
        lines.append(
            "Use `fetch_attachment(id)` to read a file attachment's text/bytes, "
            "or `list_references()` to see them all. Resolve non-file kinds with "
            "the tool provided for that kind."
        )
    return "\n".join(lines)


class ReferenceHook(HookProvider):
    """Injects the reference manifest into an in-scope agent before each invocation.

    One instance is attached per agent, carrying that agent's policy: whether it
    is in scope at all, whether it also gets inline media, and whether the
    resolver tool is available (for the manifest wording).
    """

    def __init__(self, *, enabled: bool, inline: bool, tool_enabled: bool) -> None:
        """Initialize the ReferenceHook.

        Args:
            enabled: Whether this agent is in scope for references. When
                ``False`` the hook is a no-op (``attachments: none``).
            inline: Whether to additionally prepend resolved media
                ``ContentBlock``s for a vision/doc-capable model.
            tool_enabled: Whether the built-in reference tools are registered
                (affects the manifest's guidance wording only).
        """
        self._enabled = enabled
        self._inline = inline
        self._tool_enabled = tool_enabled

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register manifest (before-invocation) + inline media (before-model) callbacks.

        The before-model callback also serves ``fetch_attachment`` requests
        (media the model asked to read), so it is registered whenever the agent
        is inline **or** has the reference tools available.
        """
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)
        if self._inline or self._tool_enabled:
            registry.add_callback(BeforeModelCallEvent, self._on_before_model)

    def _interrupt_active(self, agent: Any) -> bool:
        istate = getattr(agent, "_interrupt_state", None)
        return istate is not None and getattr(istate, "activated", False)

    def _on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        """Inject the reference manifest into the agent's system prompt.

        No-op when the agent is out of scope, when there are no references this
        run, or while an interrupt is active (mutating the prompt mid-interrupt
        would disturb the in-flight resume state, exactly as :class:`HistoryHook`
        guards against). Also arms the once-per-invocation inline injection.
        """
        # Arm inline injection for this fresh invocation (reset each turn so a
        # re-invoked agent re-materializes media once, not on every model call).
        if self._inline and not self._interrupt_active(event.agent):
            setattr(event.agent, _INLINE_DONE_ATTR, False)

        if not self._enabled or self._interrupt_active(event.agent):
            return

        references = get_references()
        if not references:
            return

        manifest = build_manifest(references, tool_enabled=self._tool_enabled)
        base = getattr(event.agent, _BASE_PROMPT_ATTR, None)
        if base is None:
            base = event.agent.system_prompt or ""
            setattr(event.agent, _BASE_PROMPT_ATTR, base)
        event.agent.system_prompt = f"{base}\n\n{manifest}" if base else manifest
        logger.debug(
            "thread=<%s>, run=<%s>, references=<%d>, inline=<%s> | injected manifest",
            get_thread_id(),
            get_run_id(),
            len(references),
            self._inline,
        )

    def _on_before_model(self, event: BeforeModelCallEvent) -> None:
        """Materialize media into a user message before the model call.

        Runs at ``BeforeModelCallEvent`` (after :class:`HistoryHook` has seeded
        ``agent.messages`` at ``BeforeInvocationEvent``), so the media survives
        seeding. For ``inline`` agents all supported media is injected once per
        invocation; for tool-enabled agents any file the model fetched via
        ``fetch_attachment`` is injected on demand. Never runs mid-interrupt.
        """
        if not self._enabled or self._interrupt_active(event.agent):
            return
        references = get_references()
        if not references:
            return
        if self._inline and not getattr(event.agent, _INLINE_DONE_ATTR, False):
            if self._inject_inline(event.agent, references):
                setattr(event.agent, _INLINE_DONE_ATTR, True)
        if self._tool_enabled:
            self._inject_requested(event.agent, references)

    def _inject_inline(self, agent: Any, references: list[Reference]) -> bool:
        """Prepend resolved media ``ContentBlock``s for an ``inline`` agent.

        Imported lazily so the reference-mode path carries no media/resolution
        cost. Only blob-capable attachment references with a supported media
        type are materialized; everything else stays pointer-only. Returns
        whether any media was injected.
        """
        from ..tools.references import resolve_inline_blocks

        return self._inject_blocks(agent, resolve_inline_blocks(references))

    def _inject_requested(self, agent: Any, references: list[Reference]) -> None:
        """Inject media the model fetched via ``fetch_attachment`` into context.

        ``fetch_attachment`` can't return media in a tool-role message on
        OpenAI-compatible providers, so it records the reference id and this
        hook materializes it into a user message (the cross-provider-safe path).
        Each id is injected at most once per agent, tracked on the agent so
        re-invocation doesn't duplicate it.
        """
        requested = get_inline_requests()
        if not requested:
            return
        injected: set[str] = getattr(agent, _FETCHED_ATTR, None) or set()
        pending = requested - injected
        if not pending:
            return

        from ..tools.references import resolve_inline_blocks

        refs = [r for r in references if r.id in pending]
        if self._inject_blocks(agent, resolve_inline_blocks(refs)):
            setattr(agent, _FETCHED_ATTR, injected | pending)

    def _inject_blocks(self, agent: Any, blocks: list[dict[str, Any]]) -> bool:
        """Prepend media ``ContentBlock``s to the latest user turn.

        Attaches to the most recent user message so tool-use/tool-result pairs
        stay intact; if there is none yet, seeds a user message with the media.
        Returns whether any media was injected.
        """
        if not blocks:
            return False
        messages = getattr(agent, "messages", None)
        if not isinstance(messages, list):
            return False
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    typed_msg = cast("dict[str, Any]", msg)
                    typed_msg["content"] = [*blocks, *content]
                    return True
        messages.insert(0, {"role": "user", "content": blocks})
        return True
