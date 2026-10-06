"""Client-driven, per-agent conversation history hook.

Seeds and captures an agent's ``messages`` around each invocation so that its
transcript lives in the AG-UI ``state`` blob (client-driven) rather than in a
process-wide singleton. This makes every non-entry agent uniformly:

- stateless on the server (nothing retained between requests),
- configurable (``history: true|false`` per agent, ``group`` for sharing),
- leak-free (a shared sub-agent singleton is reset from the client's state at
  the start of every run, so it can never carry another turn's or another
  conversation's messages).

An agent whose :class:`HistoryPolicy` keeps a masked history writes a masked
copy back to the client, while the run keeps the whole transcript.

The entry agent is exempt: its transcript is the CopilotKit chat, seeded by
ag-ui-strands from ``RunAgentInput.messages`` (see the adapter). This hook is
therefore attached to every agent EXCEPT the entry node.
"""

from __future__ import annotations

import copy
import logging
import sys
from dataclasses import dataclass
from typing import Any, Literal

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterInvocationEvent, BeforeInvocationEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from .._context import get_history_exchange, get_run_id, get_thread_id
from ..tools.wrappers import delegate_tool_names
from ._transcript import DEFAULT_MIN_CHARS, mask_transcript

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HistoryPolicy:
    """How a :class:`HistoryHook` shapes the transcript it writes back.

    Attributes:
        keep: ``"full"`` writes the transcript back as captured. ``"masked"``
            masks every finished call first, and masks a transcript stored in
            full when the agent loads it (``history.keep``).
        min_chars: Tool results shorter than this survive masking.
        keep_tools: Tools whose results survive masking. A delegate's always do.
    """

    keep: Literal["full", "masked"] = "full"
    min_chars: int = DEFAULT_MIN_CHARS
    keep_tools: frozenset[str] = frozenset()


class HistoryHook(HookProvider):
    """Seeds an agent's history from, and captures it back into, the request's
    :class:`~kaboo_workflows._context.HistoryExchange`.

    One instance is attached per agent, carrying that agent's history ``key``
    (its shared-transcript bucket or stable dot-path) and whether history is
    ``enabled`` for it.
    """

    def __init__(self, key: str, *, enabled: bool, policy: HistoryPolicy | None = None) -> None:
        """Initialize the HistoryHook.

        Args:
            key: History bucket key for this agent. Agents that share a
                ``history.group`` share a key (and therefore a transcript).
            enabled: Whether this agent remembers across turns of the same
                conversation. When ``False`` the agent is reset to an empty
                transcript before every invocation and never persists.
            policy: How the written-back transcript is shaped. Defaults to
                writing it back in full.
        """
        self._key = key
        self._enabled = enabled
        self._policy = policy or HistoryPolicy()

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """Register seed (before) and capture (after) invocation callbacks."""
        registry.add_callback(BeforeInvocationEvent, self._on_before)
        registry.add_callback(AfterInvocationEvent, self._on_after)

    def _on_before(self, event: BeforeInvocationEvent) -> None:
        """Seed ``agent.messages`` for this invocation.

        With history enabled, seed from the latest bucket state:
        ``outbound[key]`` (this run's captured transcript) if present, else
        ``inbound[key]`` (the client-supplied transcript). This single rule
        covers a fresh run (seed from client, discarding stale singleton
        content), a re-invoked agent (seed from its own captured output), and a
        grouped sibling (seed from the group's latest output). With history
        disabled, reset to an empty transcript so the agent runs fresh. A masked
        agent masks what the client sent before seeding from it, in case it was
        stored in full.

        ``BeforeInvocationEvent`` re-fires on every resume iteration, so if the
        agent is mid-interrupt we must not touch ``messages`` — seeding or
        wiping it would discard the in-flight tool state the resume depends on.
        """
        istate = getattr(event.agent, "_interrupt_state", None)
        if istate is not None and getattr(istate, "activated", False):
            logger.debug(
                "key=<%s>, thread=<%s>, run=<%s> | active interrupt; leaving messages intact",
                self._key,
                get_thread_id(),
                get_run_id(),
            )
            return

        exchange = get_history_exchange()
        if exchange is None:
            logger.debug(
                "key=<%s>, thread=<%s>, run=<%s> | no history exchange; hook is a no-op",
                self._key,
                get_thread_id(),
                get_run_id(),
            )
            return

        if not self._enabled:
            event.agent.messages = []
            logger.debug(
                "key=<%s>, thread=<%s>, run=<%s> | history disabled; reset to empty",
                self._key,
                get_thread_id(),
                get_run_id(),
            )
            return

        source = "outbound"
        seed = exchange.outbound.get(self._key)
        if seed is None:
            source = "inbound"
            seed = exchange.inbound.get(self._key)
            if seed and self._policy.keep == "masked":
                seed = self._mask(seed, event.agent)
        event.agent.messages = copy.deepcopy(seed) if seed else []
        logger.debug(
            "key=<%s>, thread=<%s>, run=<%s>, source=<%s>, seeded_messages=<%d> | seeded history",
            self._key,
            get_thread_id(),
            get_run_id(),
            source if seed else "empty",
            len(event.agent.messages),
        )

    def _on_after(self, event: AfterInvocationEvent) -> None:
        """Capture ``agent.messages`` back into the outbound buffer.

        Runs after the agent's ``conversation_manager`` has applied any
        sliding-window trim or summarization, so the reduced transcript is what
        persists to the client. A masked agent also records the masked copy the
        client is sent instead. No-op when history is disabled.
        """
        if not self._enabled:
            return
        exchange = get_history_exchange()
        if exchange is None:
            return
        exchange.outbound[self._key] = list(event.agent.messages)
        if self._policy.keep == "masked":
            exchange.persisted[self._key] = self._mask(exchange.outbound[self._key], event.agent)
        else:
            exchange.persisted.pop(self._key, None)
        logger.debug(
            "key=<%s>, thread=<%s>, run=<%s>, captured_messages=<%d>, keep=<%s> | captured history",
            self._key,
            get_thread_id(),
            get_run_id(),
            len(exchange.outbound[self._key]),
            self._policy.keep,
        )

    def _mask(self, messages: list[Any], agent: Any) -> list[Any]:
        registry = agent.tool_registry
        return mask_transcript(
            messages,
            min_chars=self._policy.min_chars,
            kept=self._policy.keep_tools | delegate_tool_names(agent),
            tools={*registry.registry, *registry.dynamic_tools},
        )
