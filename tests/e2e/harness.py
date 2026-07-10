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
from strands.multiagent.base import MultiAgentBase

from kaboo_workflows import load
from kaboo_workflows._context import (
    HistoryExchange,
    set_activity_context,
    set_history_exchange,
)
from kaboo_workflows.adapters import _strands_bridge as bridge
from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.adapters._multiagent import StrandsMultiAgent
from kaboo_workflows.adapters.agui import (
    StrandsAgent,
    _build_agui_config,
    _build_resume_responses,
    _consume_run,
    _resolve_entry_name,
)
from kaboo_workflows.config.resolvers.agents import get_agent_hook_providers
from kaboo_workflows.config.resolvers.config import _resolve_chat_output
from kaboo_workflows.types import StreamEvent

_DONE = bridge.STREAM_DONE


@dataclass
class TurnResult:
    """Outcome of one run: AG-UI events + the activity-group snapshot."""

    events: list[Any]
    groups: dict[str, dict[str, Any]]
    exchange: HistoryExchange = field(default_factory=HistoryExchange)

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

    # -- activity-group helpers --------------------------------------------

    def group_ids(self) -> list[str]:
        return sorted(self.groups.keys())

    def agent_names(self) -> set[str]:
        return {g.get("agentName") for g in self.groups.values()}

    def group(self, group_id: str) -> dict[str, Any]:
        return self.groups[group_id]

    def children_of(self, parent: str | None) -> list[str]:
        return sorted(
            gid for gid, g in self.groups.items() if g.get("parentGroup") == parent
        )


class Pipeline:
    """A resolved pipeline that can be driven turn-by-turn in one event loop."""

    def __init__(self, config_path: str, *, entry_name: str | None = None) -> None:
        self.resolved = load(str(config_path))
        self.entry = self.resolved.entry
        self.entry_name = entry_name or _resolve_entry_name(self.entry, self.resolved)
        self.registry = ActivityRegistry()
        self._event_queue: Any = None
        self._agui: Any = None

    def _ensure_wired(self) -> None:
        # Lazy so the wrapped asyncio.Queue binds to the running loop, and hooks
        # are attached exactly once for the whole conversation.
        if self._event_queue is not None:
            return
        self._event_queue = self.resolved.wire_event_queue()
        if isinstance(self.entry, MultiAgentBase):
            self._agui = StrandsMultiAgent(
                self.entry,
                name=self.entry_name,
                chat_output=_resolve_chat_output(getattr(self.resolved, "app_config", None)),
            )
        else:
            self._agui = StrandsAgent(
                agent=self.entry,
                name=self.entry_name,
                config=_build_agui_config(self.resolved),
                hooks=get_agent_hook_providers(self.entry),
            )

    async def turn(
        self,
        message: str | None = None,
        *,
        thread_id: str = "t1",
        run_id: str = "r1",
        state: dict[str, Any] | None = None,
        messages: list[Any] | None = None,
        resume: list[dict[str, Any]] | None = None,
    ) -> TurnResult:
        self._ensure_wired()
        event_queue = self._event_queue
        agui = self._agui

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
            forwarded_props={},
        )

        merged: asyncio.Queue[Any] = asyncio.Queue()

        async def pump() -> None:
            while True:
                ev = await event_queue.get()
                if ev is None:
                    break
                if isinstance(ev, StreamEvent) and ev.agent_name != self.entry_name:
                    tid = ev.data.get("thread_id") or bridge.DEFAULT_THREAD
                    self.registry.apply(tid, ev)

        pump_task = asyncio.create_task(pump())

        if isinstance(agui, StrandsMultiAgent):
            await agui.consume(input_data, merged, exchange, resume_entries=resume)
        elif resume:
            # Mirror kaboo_endpoint's resume branch: feed the interrupt responses
            # into the paused per-thread agent through ag-ui-strands' run loop.
            strands_agent = bridge.get_thread_agent(agui, thread_id)
            responses = _build_resume_responses(strands_agent, resume)
            with bridge.resume_prompt_override(strands_agent, responses):
                await _consume_run(agui, input_data, merged, exchange)
        else:
            await _consume_run(agui, input_data, merged, exchange)

        # Flush the activity queue fully, then stop the pump deterministically.
        await event_queue.close()
        await pump_task

        events: list[Any] = []
        while not merged.empty():
            item = merged.get_nowait()
            if item is _DONE:
                continue
            events.append(item)

        groups = self.registry._snapshot(thread_id).get("groups", {})
        # Deep-ish copy of the mutable group dicts so later turns can't rewrite
        # what a prior TurnResult observed.
        snapshot = {gid: dict(g) for gid, g in groups.items()}
        return TurnResult(events=events, groups=snapshot, exchange=exchange)


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
