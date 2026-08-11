"""In-process end-to-end harness.

Mirrors the production ``kaboo_endpoint`` drive path (``adapters/agui.py``): it
resolves a config, wires the activity event queue, builds the same AG-UI adapter
(:class:`StrandsMultiAgent` for a swarm/graph entry, :class:`StrandsAgent`
otherwise) and drives a run — then returns both the AG-UI event stream and the
folded activity groups.

Unlike the HTTP endpoint, the activity pump here **drains to completion** before
assertions (the endpoint cancels it at ``RUN_FINISHED``), so group state is
deterministic and race-free.

A :class:`Pipeline` is reusable across turns (multi-turn history, interrupt →
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

from kaboo_workflows._context import (
    HistoryExchange,
    SessionExchange,
    set_activity_context,
    set_forwarded_props,
    set_history_exchange,
    set_session_exchange,
)
from kaboo_workflows.adapters import _strands_bridge as bridge
from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters._multiagent import StrandsMultiAgent
from kaboo_workflows.adapters.agui import (
    _build_resume_responses,
    _build_session,
    _consume_run,
    _make_session_resolver,
    _parse_session_state,
)
from kaboo_workflows.config import parse_config_sources, resolve_infra, validate_raw_config
from kaboo_workflows.hooks import restore_session_state
from kaboo_workflows.types import StreamEvent

_DONE = bridge.STREAM_DONE


@dataclass
class TurnResult:
    """Outcome of one run: AG-UI events + the activity-group snapshot."""

    events: list[Any]
    groups: dict[str, dict[str, Any]]
    exchange: HistoryExchange = field(default_factory=HistoryExchange)
    usage_by_run: dict[str, dict[str, int]] = field(default_factory=dict)

    # -- AG-UI event helpers ------------------------------------------------

    def types(self) -> list[str]:
        return [getattr(e.type, "value", str(e.type)) for e in self.events]

    def of_type(self, agui_type: AGUIEventType) -> list[Any]:
        return [e for e in self.events if e.type == agui_type]

    def text(self) -> str:
        """Concatenate the streamed chat-reply text (TEXT_MESSAGE_CONTENT)."""
        return "".join(
            getattr(e, "delta", "")
            for e in self.events
            if e.type == AGUIEventType.TEXT_MESSAGE_CONTENT
        )

    def finished(self) -> bool:
        return any(e.type == AGUIEventType.RUN_FINISHED for e in self.events)

    def errored(self) -> bool:
        return any(e.type == AGUIEventType.RUN_ERROR for e in self.events)

    def run_outcome(self) -> Any:
        for e in self.of_type(AGUIEventType.RUN_FINISHED):
            return getattr(e, "outcome", None)
        return None

    def messages(self) -> list[Any]:
        """The last MESSAGES_SNAPSHOT, as the host would persist and replay it."""
        for event in reversed(self.events):
            if event.type == AGUIEventType.MESSAGES_SNAPSHOT:
                return list(getattr(event, "messages", None) or [])
        return []

    def state(self) -> dict[str, Any]:
        """The last STATE_SNAPSHOT, as the host would persist and replay it."""
        for event in reversed(self.events):
            if event.type == AGUIEventType.STATE_SNAPSHOT:
                snapshot = getattr(event, "snapshot", None)
                if isinstance(snapshot, dict):
                    return snapshot
        return {}

    # -- activity-group helpers --------------------------------------------

    def group_ids(self) -> list[str]:
        return sorted(self.groups.keys())

    def agent_names(self) -> set[str]:
        return {str(g["agentName"]) for g in self.groups.values() if g.get("agentName") is not None}

    def group(self, group_id: str) -> dict[str, Any]:
        return self.groups[group_id]

    def children_of(self, parent: str | None) -> list[str]:
        return sorted(gid for gid, g in self.groups.items() if g.get("parentGroup") == parent)


class Pipeline:
    """A resolved pipeline that can be driven turn-by-turn in one event loop.

    Mirrors both serving modes. By default one session serves every turn, as a
    process with a fixed config does. Pass ``session_config_key`` and each turn
    builds its own session from the config it submits in ``forwarded_props``,
    exactly as the endpoint does — which is also how a test simulates a restart,
    since a rebuilt session shares nothing with the last one.
    """

    def __init__(
        self,
        config_path: str,
        *,
        entry_name: str | None = None,
        session_config_key: str | None = None,
    ) -> None:
        self.base_raw = parse_config_sources(str(config_path))
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
        # Lazy so the wrapped asyncio.Queue binds to the running loop, and hooks
        # are attached exactly once per session.
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
        set_session_exchange(SessionExchange(inbound=_parse_session_state(input_data)))
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
            responses = _build_resume_responses(strands_agent, resume)
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


async def run_pipeline(
    config_path: str,
    message: str,
    *,
    thread_id: str = "t1",
    run_id: str = "r1",
    state: dict[str, Any] | None = None,
) -> TurnResult:
    """Build a fresh pipeline and drive a single turn (the common case)."""
    pipe = Pipeline(config_path)
    return await pipe.turn(message, thread_id=thread_id, run_id=run_id, state=state)
