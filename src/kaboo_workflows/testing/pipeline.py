"""Drive a config end to end in-process, the way the AG-UI endpoint does.

:class:`Pipeline` mirrors the production drive path of
:func:`~kaboo_workflows.adapters.create_agui_app`: it resolves a config, wires
the activity event queue, builds the same AG-UI adapter (a multi-agent adapter
for a swarm or graph entry, a single-agent adapter otherwise) and drives a run,
then returns both the AG-UI event stream and the folded activity groups as a
:class:`TurnResult`.

Unlike the HTTP endpoint, the activity pump here drains to completion before
the result is returned (the endpoint cancels it at ``RUN_FINISHED``), so group
state is deterministic and race-free.

A :class:`Pipeline` is reusable across turns (multi-turn history, interrupt and
resume) because it keeps one resolved config, one wired event queue and one
activity registry alive for the whole conversation — call ``await turn(...)``
repeatedly within a single event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ag_ui.core import EventType as AGUIEventType
from ag_ui.core import RunAgentInput, UserMessage

from .._context import (
    HistoryExchange,
    SessionExchange,
    set_activity_context,
    set_forwarded_props,
    set_history_exchange,
    set_session_exchange,
)
from ..adapters import _strands_bridge as bridge
from ..adapters._activity import ActivityRegistry
from ..adapters._interrupts import build_resume_responses
from ..adapters._multiagent import StrandsMultiAgent
from ..adapters.agui import (
    _build_session,
    _consume_run,
    _drop_replayed_tool_results,
    _make_session_resolver,
    _parse_session_state,
)
from ..config import ConfigInput, parse_config_sources, resolve_infra, validate_raw_config
from ..hooks import restore_session_state
from ..types import StreamEvent

_DONE = bridge.STREAM_DONE


@dataclass
class TurnResult:
    """Outcome of one run: the AG-UI events and the activity-group snapshot.

    Attributes:
        events: Every AG-UI event the run streamed, in order.
        groups: The activity groups (one per agent stream) as the UI would hold
            them once the run finished, keyed by group id.
        exchange: The history carried in and out of the run.
        usage_by_run: Token usage per run id.
    """

    events: list[Any]
    groups: dict[str, dict[str, Any]]
    exchange: HistoryExchange = field(default_factory=HistoryExchange)
    usage_by_run: dict[str, dict[str, int]] = field(default_factory=dict)

    def types(self) -> list[str]:
        """Return the AG-UI event types, in order.

        Returns:
            Each event's type as a string, such as ``"RUN_STARTED"``.
        """
        return [getattr(e.type, "value", str(e.type)) for e in self.events]

    def of_type(self, agui_type: AGUIEventType) -> list[Any]:
        """Return the events of one AG-UI type.

        Args:
            agui_type: The ``ag_ui.core.EventType`` to keep.

        Returns:
            The matching events, in order.
        """
        return [e for e in self.events if e.type == agui_type]

    def text(self) -> str:
        """Return the chat reply the run streamed.

        Returns:
            Every ``TEXT_MESSAGE_CONTENT`` delta, concatenated.
        """
        return "".join(
            getattr(e, "delta", "")
            for e in self.events
            if e.type == AGUIEventType.TEXT_MESSAGE_CONTENT
        )

    def finished(self) -> bool:
        """Return whether the run reached ``RUN_FINISHED``.

        Returns:
            True when a ``RUN_FINISHED`` event was streamed.
        """
        return any(e.type == AGUIEventType.RUN_FINISHED for e in self.events)

    def errored(self) -> bool:
        """Return whether the run ended in ``RUN_ERROR``.

        Returns:
            True when a ``RUN_ERROR`` event was streamed.
        """
        return any(e.type == AGUIEventType.RUN_ERROR for e in self.events)

    def run_outcome(self) -> Any:
        """Return the outcome carried by ``RUN_FINISHED``.

        Returns:
            The outcome, which holds the pending interrupts when the run
            paused, or ``None`` when the run did not finish.
        """
        for e in self.of_type(AGUIEventType.RUN_FINISHED):
            return getattr(e, "outcome", None)
        return None

    def messages(self) -> list[Any]:
        """Return the last ``MESSAGES_SNAPSHOT``, as a host would persist it.

        Returns:
            The snapshot's messages, or an empty list when none was streamed.
        """
        for event in reversed(self.events):
            if event.type == AGUIEventType.MESSAGES_SNAPSHOT:
                return list(getattr(event, "messages", None) or [])
        return []

    def state(self) -> dict[str, Any]:
        """Return the last ``STATE_SNAPSHOT``, as a host would persist it.

        Pass it back as ``state=`` on the next turn to carry history and pending
        interrupts forward, the way a client does.

        Returns:
            The snapshot, or an empty dict when none was streamed.
        """
        for event in reversed(self.events):
            if event.type == AGUIEventType.STATE_SNAPSHOT:
                snapshot = getattr(event, "snapshot", None)
                if isinstance(snapshot, dict):
                    return snapshot
        return {}

    def group_ids(self) -> list[str]:
        """Return the activity group ids.

        Returns:
            The ids, sorted.
        """
        return sorted(self.groups.keys())

    def agent_names(self) -> set[str]:
        """Return the names of the agents that produced activity.

        Returns:
            The agent names across all groups.
        """
        return {str(g["agentName"]) for g in self.groups.values() if g.get("agentName") is not None}

    def group(self, group_id: str) -> dict[str, Any]:
        """Return one activity group.

        Args:
            group_id: The group's id.

        Returns:
            The group as the UI would hold it.

        Raises:
            KeyError: No group has that id.
        """
        return self.groups[group_id]

    def children_of(self, parent: str | None) -> list[str]:
        """Return the groups nested directly under ``parent``.

        Args:
            parent: A group id, or ``None`` for the top-level groups.

        Returns:
            The child group ids, sorted.
        """
        return sorted(gid for gid, g in self.groups.items() if g.get("parentGroup") == parent)


class Pipeline:
    """A resolved config that can be driven turn by turn in one event loop."""

    def __init__(
        self,
        config: ConfigInput | list[ConfigInput],
        *,
        entry_name: str | None = None,
        session_config_key: str | None = None,
    ) -> None:
        """Resolve ``config`` once for every turn this pipeline drives.

        By default one session serves every turn, as a process with a fixed
        config does. Pass ``session_config_key`` and each turn builds its own
        session from the config it submits in ``forwarded_props``, exactly as
        the endpoint does — which is also how a test simulates a restart,
        since a rebuilt session shares nothing with the last one.

        Args:
            config: A file path, a raw YAML string, or a list of either, as
                :func:`~kaboo_workflows.config.parse_config_sources` accepts.
                With ``session_config_key`` set this is the base config each
                submitted config layers over.
            entry_name: Treat this agent as the entry when folding activity,
                instead of the config's entry.
            session_config_key: The ``forwarded_props`` key each turn's own
                config arrives under. ``None`` serves ``config`` alone.
        """
        self.base_raw = parse_config_sources(config)
        self.app_config = validate_raw_config(self.base_raw)
        self.infra = resolve_infra(self.app_config)
        self.infra.mcp_lifecycle.start(pin_clients=session_config_key is None)
        self.registry = ActivityRegistry()
        self._entry_name_override = entry_name
        self._resolver = (
            None
            if session_config_key is None
            else _make_session_resolver(
                self.base_raw, self.infra, session_config_key=session_config_key
            )
        )
        self._session: Any = None

    def _resolve_session(self, input_data: RunAgentInput) -> Any:
        """Return the session that serves ``input_data``.

        Built lazily so the wrapped ``asyncio.Queue`` binds to the running loop
        and hooks are attached exactly once per session.

        Args:
            input_data: The run's AG-UI input.

        Returns:
            A per-turn session when configs are submitted per run, otherwise the
            one shared session.
        """
        if self._resolver is not None:
            return self._resolver(input_data)
        if self._session is None:
            self._session = _build_session(self.app_config, self.infra)
        return self._session

    async def turn(
        self,
        message: str | None = None,
        *,
        thread_id: str = "t1",
        run_id: str = "r1",
        state: dict[str, Any] | None = None,
        messages: list[Any] | None = None,
        resume: list[dict[str, Any]] | None = None,
        forwarded_props: dict[str, Any] | None = None,
    ) -> TurnResult:
        """Drive one run and collect what a client would see.

        Args:
            message: The user's message. Ignored when ``messages`` is given.
            thread_id: The conversation id.
            run_id: This run's id.
            state: The client state to send, usually the previous turn's
                :meth:`TurnResult.state`, which carries history and pending
                interrupts.
            messages: A full AG-UI transcript to send instead of ``message``.
            resume: Answers to the pending interrupts, as a client sends them
                to resume a paused run.
            forwarded_props: The run's forwarded props, such as a run token or,
                with ``session_config_key``, the run's own config.

        Returns:
            The events and activity groups the run produced.

        Raises:
            LookupError: ``resume`` was given but nothing is paused on this
                thread and the state carries no gate to restore.
        """
        set_activity_context(thread_id, run_id)
        inbound: dict[str, Any] = {}
        if isinstance(state, dict) and isinstance(state.get("kaboo_history"), dict):
            inbound = state["kaboo_history"]
        exchange = HistoryExchange(inbound=inbound)
        set_history_exchange(exchange)

        if messages is None:
            messages = [UserMessage(id="m1", role="user", content=message or "")]
        input_data = RunAgentInput(
            thread_id=thread_id,
            run_id=run_id,
            state=state or {},
            messages=messages,
            tools=[],
            context=[],
            forwarded_props=forwarded_props or {},
        )
        if resume:
            # Mirror kaboo_endpoint: the client transcript carries a tool-result
            # message for the answered card; the resume payload is authoritative.
            _drop_replayed_tool_results(input_data, resume)
        set_session_exchange(
            SessionExchange(inbound=_parse_session_state(input_data), resuming=bool(resume))
        )
        set_forwarded_props(forwarded_props or {})

        session = self._resolve_session(input_data)
        event_queue = session.event_queue
        agui = session.agui_agent
        entry_name = self._entry_name_override or session.entry_name

        merged: asyncio.Queue[Any] = asyncio.Queue()

        async def pump() -> None:
            while True:
                ev = await event_queue.get()
                if ev is None:
                    break
                if not isinstance(ev, StreamEvent):
                    continue
                tid = ev.data.get("thread_id") or bridge.DEFAULT_THREAD
                if ev.agent_name != entry_name:
                    self.registry.apply(tid, ev)
                else:
                    # Mirror the endpoint pump: the entry agent's stream is not
                    # rendered, but its completions count toward the run total.
                    self.registry.apply_usage(tid, ev)

        pump_task = asyncio.create_task(pump())

        if isinstance(agui, StrandsMultiAgent):
            await agui.consume(input_data, merged, exchange, resume_entries=resume)
        elif resume:
            # Mirror kaboo_endpoint's resume branch: feed the interrupt responses
            # into the paused per-thread agent through ag-ui-strands' run loop.
            # ensure_thread_agent + restore cover the cold case, where the paused
            # clone is gone and the gate arrives on the state channel instead.
            cold = bridge.get_thread_agent(agui, thread_id) is None
            strands_agent = await bridge.ensure_thread_agent(agui, input_data)
            if not restore_session_state(strands_agent) and cold:
                # The endpoint answers RUN_ERROR / RESUME_NO_SESSION here; in-process
                # the equivalent is refusing to drive a run with nothing to resume.
                raise LookupError("no resumable agent state for thread")
            responses = build_resume_responses(bridge.pending_interrupts(strands_agent), resume)
            with bridge.resume_prompt_override(strands_agent, responses):
                await _consume_run(agui, input_data, merged, exchange)
        else:
            await _consume_run(agui, input_data, merged, exchange)

        # Flush the activity queue fully, then stop the pump deterministically.
        # A transient session is released here for the same reason the endpoint
        # releases it when the stream ends.
        if session.transient:
            await session.aclose()
        else:
            await event_queue.close()
        await pump_task

        events: list[Any] = []
        while not merged.empty():
            item = merged.get_nowait()
            if item is _DONE:
                continue
            events.append(item)

        full_snapshot = self.registry.snapshot(thread_id)
        groups = full_snapshot.get("groups", {})
        # Deep-ish copy of the mutable group dicts so later turns can't rewrite
        # what a prior TurnResult observed.
        snapshot = {gid: dict(g) for gid, g in groups.items()}
        return TurnResult(
            events=events,
            groups=snapshot,
            exchange=exchange,
            usage_by_run=full_snapshot.get("usageByRun", {}),
        )

    def close(self) -> None:
        """Stop the MCP servers and clients the config started."""
        self.infra.mcp_lifecycle.stop()


async def run_pipeline(
    config: ConfigInput | list[ConfigInput],
    message: str,
    *,
    thread_id: str = "t1",
    run_id: str = "r1",
    state: dict[str, Any] | None = None,
) -> TurnResult:
    """Build a fresh pipeline, drive one turn, and close it.

    Args:
        config: A file path, a raw YAML string, or a list of either.
        message: The user's message.
        thread_id: The conversation id.
        run_id: The run's id.
        state: The client state to send.

    Returns:
        The events and activity groups the run produced.
    """
    pipe = Pipeline(config)
    try:
        return await pipe.turn(message, thread_id=thread_id, run_id=run_id, state=state)
    finally:
        pipe.close()
