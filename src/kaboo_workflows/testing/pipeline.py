"""Drive a config end to end in-process, the way the AG-UI endpoint does.

:class:`Pipeline` mirrors the production drive path of
:func:`~kaboo_workflows.adapters.create_agui_app`: it resolves a config, builds
the same AG-UI adapter and drives a run, with the sub-agents' events forwarded
into it the same way, then returns the AG-UI events as a :class:`TurnResult`,
along with the sub-agent groups a client would fold them into.

A :class:`Pipeline` is reusable across turns (multi-turn history, interrupt and
resume) because it keeps one resolved config and every event the conversation
has streamed — call ``await turn(...)`` repeatedly within a single event loop.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any

import jsonpatch
from ag_ui.core import (
    AssistantMessage,
    FunctionCall,
    ResumeEntry,
    RunAgentInput,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from ag_ui.core import EventType as AGUIEventType

from .._context import (
    HistoryExchange,
    SessionExchange,
    set_activity_context,
    set_forwarded_props,
    set_history_exchange,
    set_references,
    set_session_exchange,
)
from ..adapters import _strands_bridge as bridge
from ..adapters._a2ui_actions import take_a2ui_action
from ..adapters._client_tools import equip_entry_agent, prepare_run, take_browser_answers
from ..adapters._sessions import inbound_sessions
from ..adapters._subagents import RunEvents, fold_subagents, split_subagent_messages
from ..adapters.agui import (
    _consume_run,
    _ConversationSessions,
    _make_session_resolver,
    _parse_references,
    _prepare_resume,
    open_state_channel,
)
from ..config import ConfigInput, parse_config_sources, resolve_infra, validate_raw_config

_DONE = bridge.STREAM_DONE


@dataclass
class TurnResult:
    """Outcome of one run: the AG-UI events and the sub-agent groups.

    Attributes:
        events: Every AG-UI event the run streamed, in order.
        groups: One group per sub-agent invocation of the thread so far,
            keyed by its ``subagentRunId``, as a client folds them (see
            :func:`~kaboo_workflows.adapters._subagents.fold_subagents`).
        exchange: The history carried in and out of the run.
        usage_by_run: The run's token totals per run id, from ``RUN_FINISHED``.
        sent: The messages the client sent with the run.
    """

    events: list[Any]
    groups: dict[str, dict[str, Any]]
    exchange: HistoryExchange = field(default_factory=HistoryExchange)
    usage_by_run: dict[str, dict[str, int]] = field(default_factory=dict)
    sent: list[Any] = field(default_factory=list)

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
            Every ``TEXT_MESSAGE_CONTENT`` delta of the entry agent,
            concatenated; sub-agents' text belongs to their cards.
        """
        return "".join(
            getattr(e, "delta", "")
            for e in self.events
            if e.type == AGUIEventType.TEXT_MESSAGE_CONTENT
            and not getattr(e, "subagent_run_id", None)
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
        """Return the transcript a client holds after the run.

        The messages it sent, followed by the ones the run's text, tool-call
        and tool-result events build, sub-agents' included. Pass it back as
        ``messages=`` on the next turn, the way a client does.

        Returns:
            The messages, in order.
        """
        messages = [message.model_copy(deep=True) for message in self.sent]
        by_id: dict[str, Any] = {message.id: message for message in messages}
        calls: dict[str, ToolCall] = {}

        def assistant(message_id: str, subagent_run_id: str | None) -> Any:
            if message_id not in by_id:
                by_id[message_id] = AssistantMessage(
                    id=message_id, role="assistant", subagent_run_id=subagent_run_id
                )
                messages.append(by_id[message_id])
            return by_id[message_id]

        for event in self.events:
            if event.type == AGUIEventType.TEXT_MESSAGE_START:
                assistant(event.message_id, event.subagent_run_id)
            elif event.type == AGUIEventType.TEXT_MESSAGE_CONTENT:
                message = by_id[event.message_id]
                message.content = (message.content or "") + event.delta
            elif event.type == AGUIEventType.TOOL_CALL_START:
                message = assistant(
                    event.parent_message_id or event.tool_call_id, event.subagent_run_id
                )
                call = ToolCall(
                    id=event.tool_call_id,
                    type="function",
                    function=FunctionCall(name=event.tool_call_name, arguments=""),
                )
                message.tool_calls = [*(message.tool_calls or []), call]
                calls[event.tool_call_id] = call
            elif event.type == AGUIEventType.TOOL_CALL_ARGS and event.tool_call_id in calls:
                calls[event.tool_call_id].function.arguments += event.delta
            elif event.type == AGUIEventType.TOOL_CALL_RESULT and event.message_id not in by_id:
                by_id[event.message_id] = ToolMessage(
                    id=event.message_id,
                    role="tool",
                    content=event.content,
                    tool_call_id=event.tool_call_id,
                    subagent_run_id=event.subagent_run_id,
                )
                messages.append(by_id[event.message_id])
        return messages

    def state(self) -> dict[str, Any]:
        """Return the state the run left, as a host would persist it.

        The last ``STATE_SNAPSHOT`` with every ``STATE_DELTA`` after it applied.
        Pass it back as ``state=`` on the next turn to carry history and pending
        interrupts forward, the way a client does.

        Returns:
            The state, or an empty dict when none was streamed.
        """
        state: dict[str, Any] = {}
        for event in self.events:
            if event.type == AGUIEventType.STATE_SNAPSHOT:
                snapshot = getattr(event, "snapshot", None)
                state = copy.deepcopy(snapshot) if isinstance(snapshot, dict) else {}
            elif event.type == AGUIEventType.STATE_DELTA:
                delta = event.model_dump(by_alias=True, exclude_none=True)["delta"]
                state = jsonpatch.apply_patch(state, delta)
        return state

    def group_ids(self) -> list[str]:
        """Return the sub-agent group ids.

        Returns:
            The ids, sorted.
        """
        return sorted(self.groups.keys())

    def agent_names(self) -> set[str]:
        """Return the names of the sub-agents that ran.

        Returns:
            The agent names across all groups.
        """
        return {str(g["agentName"]) for g in self.groups.values() if g.get("agentName") is not None}

    def group(self, group_id: str) -> dict[str, Any]:
        """Return one sub-agent group.

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
            session_config_key: The ``forwarded_props`` key each turn's own
                config arrives under. ``None`` serves ``config`` alone.
        """
        self.base_raw = parse_config_sources(config)
        self.app_config = validate_raw_config(self.base_raw)
        self.infra = resolve_infra(self.app_config)
        self.infra.mcp_lifecycle.start(pin_clients=session_config_key is None)
        self._events: dict[str, list[Any]] = {}
        self._usage_by_run: dict[str, dict[str, int]] = {}
        self._resolver = (
            None
            if session_config_key is None
            else _make_session_resolver(
                self.base_raw, self.infra, session_config_key=session_config_key
            )
        )
        self._conversations = _ConversationSessions(self.app_config, self.infra)

    def _resolve_session(self, input_data: RunAgentInput) -> Any:
        """Return the session that serves ``input_data``.

        Built lazily so the wrapped ``asyncio.Queue`` binds to the running loop
        and hooks are attached exactly once per session.

        Args:
            input_data: The run's AG-UI input.

        Returns:
            A per-turn session when configs are submitted per run, otherwise the
            conversation's own session, as the endpoint serves it.
        """
        if self._resolver is not None:
            return self._resolver(input_data)
        return self._conversations(input_data)

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
        tools: list[Any] | None = None,
        context: list[Any] | None = None,
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
            tools: The browser tools the client declares, as AG-UI ``Tool``s.
            context: The client's context items, as AG-UI ``Context``s.

        Returns:
            The events and activity groups the run produced.

        Raises:
            LookupError: ``resume`` was given but nothing is paused on this
                thread and the state carries no gate to restore.
        """
        set_activity_context(thread_id, run_id)
        history, paused = inbound_sessions(state)
        exchange = HistoryExchange(inbound=history)
        set_history_exchange(exchange)

        if messages is None:
            messages = [UserMessage(id="m1", role="user", content=message or "")]
        input_data = RunAgentInput(
            thread_id=thread_id,
            run_id=run_id,
            state=state or {},
            messages=messages,
            tools=tools or [],
            context=context or [],
            forwarded_props=forwarded_props or {},
            resume=[ResumeEntry.model_validate(entry) for entry in resume] if resume else None,
        )
        sent = list(input_data.messages)
        take_a2ui_action(input_data)
        carried = split_subagent_messages(input_data)
        browser_answers = take_browser_answers(input_data, carried)
        set_session_exchange(
            SessionExchange(inbound=paused, resuming=bool(resume or browser_answers))
        )
        set_forwarded_props(forwarded_props or {})
        set_references(_parse_references(input_data))

        session = self._resolve_session(input_data)
        agui = session.agui_agent
        browser_tools = prepare_run(input_data, session)
        merged: asyncio.Queue[Any] = asyncio.Queue()
        run_events = RunEvents(
            merged, carried, subagents=not session.orchestrated, browser_tools=browser_tools
        )
        session.event_queue.flush()
        session.event_queue.forward_to(run_events.sink)
        open_state_channel(input_data, session, run_events)

        try:
            if (resume or browser_answers) and not session.orchestrated:
                # Mirror kaboo_endpoint: ready the paused agent, then hand the
                # resume to ag-ui-strands as it came.
                if not await _prepare_resume(agui, input_data, browser_answers):
                    # The endpoint answers RUN_ERROR / RESUME_NO_SESSION here;
                    # in-process the equivalent is refusing to drive the run.
                    raise LookupError("no resumable agent state for thread")
            await equip_entry_agent(input_data, session)
            await _consume_run(agui, input_data, merged, exchange)
        finally:
            session.event_queue.forward_to(None)
            if session.transient:
                await session.aclose()

        events: list[Any] = []
        while not merged.empty():
            item = merged.get_nowait()
            if item is not _DONE:
                events.extend(run_events.emit(item))
        conversation = self._events.setdefault(thread_id, [])
        conversation.extend(events)
        for finished in (e for e in events if e.type == AGUIEventType.RUN_FINISHED):
            totals: dict[str, int] = {}
            for entry in getattr(finished, "usage", None) or []:
                for key, value in entry.model_dump(by_alias=True, exclude_none=True).items():
                    if isinstance(value, int):
                        totals[key] = totals.get(key, 0) + value
            self._usage_by_run[finished.run_id] = totals
        return TurnResult(
            events=events,
            groups=fold_subagents(conversation),
            exchange=exchange,
            usage_by_run=dict(self._usage_by_run),
            sent=sent,
        )

    def forget(self, thread_id: str) -> None:
        """Drop a conversation's session and events, as a process that never saw it.

        Args:
            thread_id: The conversation id.
        """
        self._conversations.forget(thread_id)
        self._events.pop(thread_id, None)

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
