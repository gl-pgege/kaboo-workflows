"""Carry agent session state on the AG-UI state channel.

An interrupt — an approval gate, an ``ask_user`` form — pauses a run. What the
agent was waiting for lives in ``agent._interrupt_state``, in memory, on the
per-thread clone. Nothing persists it unless the config declares a
``session_manager``, so restarting the runtime silently drops every pending
approval: the analyst clicks approve and it reaches an agent that has no idea
what is being approved.

This hook removes the need for an external store by treating the state the same
way kaboo already treats per-agent history — as client-driven state on the wire.
``RunAgentInput.state['kaboo_session']`` comes in, the enricher in the AG-UI
adapter writes it back into the outgoing ``STATE_SNAPSHOT``, and the host
persists it alongside the transcript it is already keeping. A restart costs
nothing: the next turn restores the paused gate from the state the host replayed.

That is only sound because the AG-UI client is a server. Behind kaboo-runtime
the state channel is server-to-server, so its content is as trustworthy as the
host's own database. A host exposing this endpoint straight to a browser must
disable the hook, because a client that can edit gate state can approve its own
interrupts.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TypeGuard

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeInvocationEvent
from strands.interrupt import _InterruptState

from .._context import get_session_exchange

if sys.version_info >= (3, 12):
    from typing import override
else:
    from typing_extensions import override

logger = logging.getLogger(__name__)

_REQUIRED_KEYS = ("interrupts", "context", "activated")


def _is_interrupt_state(value: Any) -> TypeGuard[dict[str, Any]]:
    """True when *value* has the shape ``_InterruptState.from_dict`` requires.

    ``from_dict`` indexes all three keys directly, so a partial dict raises
    ``KeyError``. State arrives from the host, and an older host may not send
    this key at all, so validate rather than trust.
    """
    return isinstance(value, dict) and all(key in value for key in _REQUIRED_KEYS)


def restore_session_state(agent: Any) -> bool:
    """Seed *agent* with the interrupt state the client sent, if any.

    Returns whether anything was restored. Idempotent, so the adapter can call it
    on the resume path — where the state is needed *before* the run starts, to
    build the interrupt responses — and the hook can call it again for a fresh
    run without either clobbering the other.
    """
    exchange = get_session_exchange()
    if exchange is None:
        return False
    exchange.agent = agent
    state = exchange.inbound.get("interrupt_state")
    if not _is_interrupt_state(state):
        return False
    # A warm agent's own state is at least as fresh as the client's, since the
    # client got it from us. Restoring over it would let a stale snapshot
    # resurrect an answered gate, so only seed a clean agent.
    current = getattr(agent, "_interrupt_state", None)
    if current is not None and (current.activated or current.interrupts):
        return False
    try:
        agent._interrupt_state = _InterruptState.from_dict(state)
    except Exception:
        logger.exception("kaboo_session restore failed; starting with clean state")
        return False
    logger.debug(
        "kaboo_session restored | activated=%s interrupts=%s",
        state.get("activated"),
        list(state.get("interrupts") or {}),
    )
    return True


class SessionStateHook(HookProvider):
    """Restore client-supplied interrupt state onto the executing agent.

    Also records that agent on the request's
    :class:`~kaboo_workflows._context.SessionExchange`, so the adapter can
    serialize its state once the run has paused or finished.
    """

    @override
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_before_invocation)

    def _on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        restore_session_state(event.agent)


def session_state_snapshot() -> dict[str, Any] | None:
    """Serialize the current request's agent state for the outgoing snapshot.

    Returns ``None`` when there is nothing worth sending: no exchange, no agent
    reached invocation, or the agent is not holding an interrupt. Writing an
    empty state would still be correct, but it would add a key to every
    snapshot of every run that never pauses.
    """
    exchange = get_session_exchange()
    if exchange is None or exchange.agent is None:
        return None
    state = getattr(exchange.agent, "_interrupt_state", None)
    if state is None:
        return None
    try:
        serialized = state.to_dict()
    except Exception:
        logger.exception("kaboo_session capture failed; snapshot left unchanged")
        return None
    if not serialized.get("activated") and not serialized.get("interrupts"):
        # Nothing pending. Send it anyway if the client sent state, so a
        # resolved gate is cleared rather than replayed on the next turn.
        if not _is_interrupt_state(exchange.inbound.get("interrupt_state")):
            return None
    return {"interrupt_state": serialized}
