"""A conversation's sessions in the state channel: ``kaboo_sessions``.

Every agent's memory and the place a paused run stopped live under one
server-owned state key. The client sends it back with every run, kaboo-runtime
keeps it from the browser, and each run ends by writing it back as one
``STATE_DELTA``:

- ``history``: each agent's transcript, by history bucket (see
  :class:`~kaboo_workflows.hooks.HistoryHook`).
- ``paused``: the paused agent's checkpoint, and the place of every delegate it
  left paused, so any process can resume the run.
- ``approvals``: the approvals the run waits on, which a host matches the
  person's decisions against.
"""

from __future__ import annotations

from typing import Any

from ag_ui.core import EventType, StateDeltaEvent

from .._context import HistoryExchange, get_session_exchange
from ..hooks import session_state_snapshot
from . import _strands_bridge as bridge
from ._interrupts import approval_descriptors

SESSIONS_KEY = "kaboo_sessions"


def inbound_sessions(state: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The transcripts and the paused checkpoint a run's state carries.

    Returns:
        ``(history, paused)``, each empty when the state carries none.
    """
    sessions = state.get(SESSIONS_KEY) if isinstance(state, dict) else None
    if not isinstance(sessions, dict):
        return {}, {}
    history = sessions.get("history")
    paused = sessions.get("paused")
    return (
        history if isinstance(history, dict) else {},
        paused if isinstance(paused, dict) else {},
    )


def sessions_delta(exchange: HistoryExchange) -> StateDeltaEvent | None:
    """The run's sessions, as the ``STATE_DELTA`` that writes them back.

    ``None`` when the run neither holds nor carried anything to keep.
    """
    session = get_session_exchange()
    history = exchange.written_back()
    paused = session_state_snapshot()
    carried = bool(exchange.inbound) or bool(session is not None and session.inbound)
    if not history and paused is None and not carried:
        return None
    value: dict[str, Any] = {"history": history}
    if paused is not None:
        value["paused"] = paused
        agent = session.agent if session is not None else None
        if agent is not None and (
            approvals := approval_descriptors(bridge.pending_interrupts(agent).values())
        ):
            value["approvals"] = approvals
    return StateDeltaEvent(
        type=EventType.STATE_DELTA,
        delta=[{"op": "add", "path": f"/{SESSIONS_KEY}", "value": value}],
    )
