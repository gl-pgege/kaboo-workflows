"""Headless run capture: drive one item through the production wire path.

:class:`EvalPipeline` mirrors the AG-UI endpoint's drive path (resolve config →
build session → consume run → fold activity), the same pattern as the e2e test
harness — so an eval run exercises exactly what production serves: the same
agents, orchestration, MCP tools, hooks, and event stream.
"""

from __future__ import annotations

import asyncio
import time
import uuid
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
    set_inline_requests,
    set_references,
    set_session_exchange,
)
from ..adapters import _strands_bridge as bridge
from ..adapters._activity import ActivityRegistry
from ..adapters._multiagent import StrandsMultiAgent
from ..adapters.agui import _build_session, _consume_run, _parse_session_state
from ..config import parse_config_sources, resolve_infra, validate_raw_config
from ..types import StreamEvent
from .dataset import EvalItem

_DEFAULT_TIMEOUT_S = 600.0


@dataclass
class ToolInvocation:
    """One trajectory step: a tool call, or a sub-agent invocation.

    Delegations/handoffs surface as activity groups rather than tool entries,
    so each group contributes a step with ``kind="agent"`` (named after the
    invoked agent) followed by that agent's own tool calls (``kind="tool"``).
    """

    agent: str
    name: str
    status: str
    input: Any = None
    kind: str = "tool"


@dataclass
class RunCapture:
    """Everything a scorer can look at from one headless run."""

    item_id: str
    text: str = ""
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    tools: list[ToolInvocation] = field(default_factory=list)
    structured_outputs: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_s: float = 0.0
    error: str | None = None

    def tool_names(self) -> list[str]:
        """Tool names in call order (successful or not)."""
        return [t.name for t in self.tools]


def _capture_from_state(
    item_id: str,
    *,
    text: str,
    thread_state: dict[str, Any],
    run_id: str,
    latency_s: float,
    error: str | None,
) -> RunCapture:
    groups: dict[str, dict[str, Any]] = thread_state.get("groups", {})
    tools: list[ToolInvocation] = []
    structured: list[dict[str, Any]] = []
    for group in groups.values():
        agent = str(group.get("agentName", ""))
        tools.append(
            ToolInvocation(
                agent=str(group.get("parentGroup") or ""),
                name=agent,
                status=str(group.get("status", "")),
                input=group.get("task"),
                kind="agent",
            )
        )
        for tool in group.get("tools", []):
            tools.append(
                ToolInvocation(
                    agent=agent,
                    name=str(tool.get("toolName", "")),
                    status=str(tool.get("status", "")),
                    input=tool.get("toolInput"),
                )
            )
        if isinstance(group.get("structuredOutput"), dict):
            structured.append(group["structuredOutput"])
    usage = thread_state.get("usageByRun", {}).get(run_id, {})
    return RunCapture(
        item_id=item_id,
        text=text,
        groups={gid: dict(g) for gid, g in groups.items()},
        tools=tools,
        structured_outputs=structured,
        usage=dict(usage),
        latency_s=latency_s,
        error=error,
    )


class EvalPipeline:
    """A resolved workflow that runs dataset items headlessly, one at a time.

    Holds one resolved config and one started MCP lifecycle for the whole
    eval run (items stream through it sequentially — no whole-run buffering).
    Call :meth:`close` (or use ``async with``) when done.
    """

    def __init__(self, config_path: str) -> None:
        """Resolve *config_path* and start its MCP servers/clients."""
        self.base_raw = parse_config_sources(str(config_path))
        self.app_config = validate_raw_config(self.base_raw)
        self.infra = resolve_infra(self.app_config)
        self.infra.mcp_lifecycle.start(pin_clients=True)
        self.registry = ActivityRegistry()
        self._session: Any = None

    async def __aenter__(self) -> EvalPipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Stop the MCP lifecycle."""
        self.infra.mcp_lifecycle.stop()

    def _session_for_run(self) -> Any:
        if self._session is None:
            self._session = _build_session(self.app_config, self.infra)
        return self._session

    async def run_item(self, item: EvalItem) -> RunCapture:
        """Run one dataset item and capture its outcome.

        Each item gets a fresh thread (no cross-item history), a unique run id,
        and a wall-clock timeout (``item.timeout_s`` or 600s). Run errors are
        captured on the :class:`RunCapture` rather than raised, so one broken
        item never aborts the dataset.
        """
        thread_id = f"eval-{item.id}-{uuid.uuid4().hex[:8]}"
        run_id = f"run-{item.id}"
        set_activity_context(thread_id, run_id, run_id)
        exchange = HistoryExchange()
        set_history_exchange(exchange)
        set_references([])
        set_forwarded_props(item.forwarded_props or {})
        set_inline_requests(set())

        input_data = RunAgentInput(
            thread_id=thread_id,
            run_id=run_id,
            state=item.state or {},
            messages=[UserMessage(id=f"m-{item.id}", role="user", content=item.input)],
            tools=[],
            context=[],
            forwarded_props=item.forwarded_props or {},
        )
        set_session_exchange(SessionExchange(inbound=_parse_session_state(input_data)))

        session = self._session_for_run()
        event_queue = session.event_queue
        entry_name = session.entry_name

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
                    self.registry.apply_usage(tid, ev)

        pump_task = asyncio.create_task(pump())
        started = time.monotonic()
        error: str | None = None
        try:
            agui = session.agui_agent
            timeout = item.timeout_s or _DEFAULT_TIMEOUT_S
            if isinstance(agui, StrandsMultiAgent):
                await asyncio.wait_for(
                    agui.consume(input_data, merged, exchange, resume_entries=None),
                    timeout=timeout,
                )
            else:
                await asyncio.wait_for(_consume_run(agui, input_data, merged, exchange), timeout)
        except TimeoutError:
            error = f"timed out after {item.timeout_s or _DEFAULT_TIMEOUT_S:.0f}s"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        latency_s = time.monotonic() - started

        # Flush the activity stream deterministically before reading state.
        await event_queue.close()
        await pump_task

        text_parts: list[str] = []
        while not merged.empty():
            event = merged.get_nowait()
            if event is bridge.STREAM_DONE:
                continue
            event_type = getattr(event, "type", None)
            if event_type == AGUIEventType.TEXT_MESSAGE_CONTENT:
                text_parts.append(getattr(event, "delta", "") or "")
            elif event_type == AGUIEventType.RUN_ERROR and error is None:
                error = getattr(event, "message", None) or "RUN_ERROR"

        # The shared session's event queue is closed by the flush above; drop
        # the session so the next item wires a fresh queue on the live loop.
        self._session = None

        return _capture_from_state(
            item.id,
            text="".join(text_parts),
            thread_state=self.registry.snapshot(thread_id),
            run_id=run_id,
            latency_s=latency_s,
            error=error,
        )
