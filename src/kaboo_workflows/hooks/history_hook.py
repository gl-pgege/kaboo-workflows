"""Client-driven, per-agent conversation history hook.

Seeds and captures an agent's ``messages`` around each invocation so that its
transcript lives in the AG-UI ``state`` blob (client-driven) rather than in a
process-wide singleton. This makes every non-entry agent uniformly:

- stateless on the server (nothing retained between requests),
- configurable (``history: true|false`` per agent, ``group`` for sharing),
- leak-free (a shared sub-agent singleton is reset from the client's state at
  the start of every run, so it can never carry another turn's or another
  conversation's messages).

The entry agent is exempt: its transcript is the CopilotKit chat, seeded by
ag-ui-strands from ``RunAgentInput.messages`` (see the adapter). This hook is
therefore attached to every agent EXCEPT the entry node.
"""

from __future__ import annotations

import copy
import logging
import sys
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import AfterInvocationEvent, BeforeInvocationEvent

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

from .._context import get_history_exchange, get_run_id, get_thread_id

logger = logging.getLogger(__name__)


class HistoryHook(HookProvider):
    """Seeds an agent's history from, and captures it back into, the request's
    :class:`~kaboo_workflows._context.HistoryExchange`.

    One instance is attached per agent, carrying that agent's history ``key``
    (its shared-transcript bucket or stable dot-path) and whether history is
    ``enabled`` for it.
    """

    def __init__(self, key: str, *, enabled: bool) -> None:
        """Initialize the HistoryHook.

        Args:
            key: History bucket key for this agent. Agents that share a
                ``history.group`` share a key (and therefore a transcript).
            enabled: Whether this agent remembers across turns of the same
                conversation. When ``False`` the agent is reset to an empty
                transcript before every invocation and never persists.
        """
        self._key = key
        self._enabled = enabled

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
        disabled, reset to an empty transcript so the agent runs fresh.

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
        persists to the client. No-op when history is disabled.
        """
        if not self._enabled:
            return
        exchange = get_history_exchange()
        if exchange is None:
            return
        exchange.outbound[self._key] = list(event.agent.messages)
        logger.debug(
            "key=<%s>, thread=<%s>, run=<%s>, captured_messages=<%d> | captured history",
            self._key,
            get_thread_id(),
            get_run_id(),
            len(exchange.outbound[self._key]),
        )
