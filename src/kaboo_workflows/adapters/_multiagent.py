"""First-class AG-UI adapter for Swarm/Graph orchestration entries.

``ag-ui-strands`` ``StrandsAgent`` is hard-coupled to a single ``strands.Agent``
and cannot drive a ``Swarm``/``Graph`` (it never unwraps the
``multiagent_node_stream`` envelope, and ignores ``multiagent_node_interrupt``).
This module is kaboo's own multi-agent run loop: it drives
``orchestrator.stream_async(task)`` directly, streams a designated node's text
as the assistant reply, maps orchestrator-level interrupts to AG-UI HITL, and
emits the terminal result — while the shared ``EventPublisher``/``EventQueue``
(already wired onto every member) continues to feed the ``ACTIVITY_SNAPSHOT``
events interleaved on the run stream.

Chat voice: only the ``chat_output`` node's text becomes the chat bubble. Every
member run (including ``chat_output``) still renders as an activity card, so the
answer is never shown twice — the card carries tools/timeline, the bubble
carries the text.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from ag_ui.core import (
    EventType as AGUIEventType,
)
from ag_ui.core import (
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)

from . import _strands_bridge as bridge
from ._interrupts import map_strands_interrupt_to_agui as _map_strands_interrupt_to_agui

if TYPE_CHECKING:
    from ag_ui.core import RunAgentInput
    from strands.multiagent.base import MultiAgentBase

    from kaboo_workflows._context import HistoryExchange

logger = logging.getLogger(__name__)

_DONE = bridge.STREAM_DONE


class StrandsMultiAgent:
    """Drive a Swarm/Graph orchestration as a first-class AG-UI chat entry.

    A single wired orchestrator instance is shared across threads (its members
    carry the ``EventPublisher`` hooks that power the activity snapshots, so a
    fresh per-thread instance would lose that wiring). Two measures make that
    safe across conversations:

    - **Single-flight execution**: a lock serializes runs, so two threads never
      drive the orchestrator (and its members' message state) concurrently.
    - **Per-thread interrupt state**: each run installs its own thread's
      ``_InterruptState`` on the orchestrator and parks it again afterwards,
      so a gate paused on one conversation is invisible to — and cannot be
      clobbered by — runs on any other. strands restores member state from the
      interrupt context on resume, so the parked state carries everything a
      paused thread needs.
    """

    def __init__(
        self,
        orchestrator: MultiAgentBase,
        *,
        name: str,
        chat_output: str | None,
    ) -> None:
        self.orchestrator = orchestrator
        self.name = name
        self.chat_output = chat_output
        self._run_lock = asyncio.Lock()
        self._parked_interrupts: dict[str, Any] = {}

    # -- interrupt helpers (thread-scoped, reading the parked state) --------

    def is_interrupt_active(self, thread_id: str) -> bool:
        state = self._parked_interrupts.get(thread_id)
        return bool(state is not None and getattr(state, "activated", False))

    def pending_interrupts(self, thread_id: str, *, unresolved_only: bool = True) -> dict[str, Any]:
        return bridge.pending_interrupts_of(
            self._parked_interrupts.get(thread_id), unresolved_only=unresolved_only
        )

    def deactivate_interrupts(self, thread_id: str) -> None:
        state = self._parked_interrupts.pop(thread_id, None)
        if state is not None and getattr(state, "activated", False):
            state.deactivate()

    # -- run loop ----------------------------------------------------------

    async def consume(
        self,
        input_data: RunAgentInput,
        merged: asyncio.Queue[Any],
        exchange: HistoryExchange,
        *,
        resume_entries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Drive one orchestration run and forward AG-UI events onto *merged*.

        Ends by putting the ``_DONE`` sentinel so the SSE pump stops. Mirrors
        the contract of :func:`kaboo_workflows.adapters.agui._consume_run`.
        """
        thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
        run_id = input_data.run_id or str(uuid.uuid4())
        message_id = str(uuid.uuid4())

        logger.info(
            "multiagent.consume START name=%s thread_id=%s run_id=%s chat_output=%s "
            "resume=%s orchestrator=%s",
            self.name,
            thread_id,
            run_id,
            self.chat_output,
            resume_entries is not None,
            type(self.orchestrator).__name__,
        )

        try:
            await merged.put(
                RunStartedEvent(type=AGUIEventType.RUN_STARTED, thread_id=thread_id, run_id=run_id)
            )
            async with self._run_lock:
                await self._run(input_data, merged, exchange, resume_entries, run_id, message_id)
        except Exception as exc:  # noqa: BLE001 - surface any run failure as RUN_ERROR
            logger.error("multi-agent run failed: %s", exc, exc_info=True)
            await merged.put(
                RunErrorEvent(type=AGUIEventType.RUN_ERROR, message=str(exc), code="AGENT_ERROR")
            )
        finally:
            await merged.put(_DONE)

    async def _run(
        self,
        input_data: RunAgentInput,
        merged: asyncio.Queue[Any],
        exchange: HistoryExchange,
        resume_entries: list[dict[str, Any]] | None,
        run_id: str,
        message_id: str,
    ) -> None:
        thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
        # Install this thread's interrupt state for the duration of the run;
        # it is parked again (or discarded once settled) in the finally below.
        bridge.swap_interrupt_state(self.orchestrator, self._parked_interrupts.pop(thread_id, None))
        try:
            if resume_entries is not None:
                task: Any = _build_resume_responses(self.orchestrator, resume_entries)
            else:
                task = _extract_user_task(input_data)
            logger.info("multiagent.consume task=%r", task)

            node_text: dict[str | None, str] = {}
            last_completed: str | None = None
            text_started = False
            final_result: Any = None
            event_type_counts: dict[str, int] = {}

            async for event in self.orchestrator.stream_async(task):
                etype = event.get("type") if isinstance(event, dict) else None
                key = str(etype)
                event_type_counts[key] = event_type_counts.get(key, 0) + 1

                if etype == "multiagent_node_stream":
                    node_id = event.get("node_id")
                    inner = event.get("event") or {}
                    delta = inner.get("data") if isinstance(inner, dict) else None
                    if delta:
                        if node_id not in node_text:
                            logger.info(
                                "multiagent.node_stream first-delta node_id=%s is_chat_output=%s",
                                node_id,
                                node_id == self.chat_output,
                            )
                        node_text[node_id] = node_text.get(node_id, "") + str(delta)
                        if self.chat_output is not None and node_id == self.chat_output:
                            if not text_started:
                                await merged.put(
                                    TextMessageStartEvent(
                                        type=AGUIEventType.TEXT_MESSAGE_START,
                                        message_id=message_id,
                                        role="assistant",
                                    )
                                )
                                text_started = True
                            await merged.put(
                                TextMessageContentEvent(
                                    type=AGUIEventType.TEXT_MESSAGE_CONTENT,
                                    message_id=message_id,
                                    delta=str(delta),
                                )
                            )
                elif etype == "multiagent_node_stop":
                    last_completed = event.get("node_id") or last_completed
                    logger.info("multiagent.node_stop node_id=%s", event.get("node_id"))

                if isinstance(event, dict) and "result" in event:
                    final_result = event["result"]

            logger.info(
                "multiagent.consume STREAM-DONE name=%s event_counts=%s nodes_streamed=%s "
                "last_completed=%s text_started=%s has_final_result=%s interrupt_active=%s",
                self.name,
                event_type_counts,
                list(node_text.keys()),
                last_completed,
                text_started,
                final_result is not None,
                bridge.is_interrupt_active(self.orchestrator),
            )

            if text_started:
                await merged.put(
                    TextMessageEndEvent(type=AGUIEventType.TEXT_MESSAGE_END, message_id=message_id)
                )

            # History write-back so members' client-driven transcripts persist.
            snapshot = _history_snapshot(input_data, exchange)
            if snapshot is not None:
                await merged.put(
                    StateSnapshotEvent(type=AGUIEventType.STATE_SNAPSHOT, snapshot=snapshot)
                )

            if bridge.is_interrupt_active(self.orchestrator):
                pending = bridge.pending_interrupts(self.orchestrator, unresolved_only=True)
                agui_interrupts = [
                    _map_strands_interrupt_to_agui(intr) for intr in pending.values()
                ]
                await merged.put(
                    RunFinishedEvent(
                        type=AGUIEventType.RUN_FINISHED,
                        thread_id=thread_id,
                        run_id=run_id,
                        outcome=RunFinishedInterruptOutcome.model_validate(
                            {"interrupts": agui_interrupts}
                        ),
                    )
                )
                return

            # Normal completion. If nothing streamed live (no chat_output, or
            # the node produced no token deltas), emit the chosen node's final
            # text as a single assistant message so the chat always has a reply.
            if not text_started:
                target = self.chat_output or last_completed
                text = node_text.get(target or "", "")
                if not text and final_result is not None:
                    text = _result_text(final_result, target)
                if text:
                    await merged.put(
                        TextMessageStartEvent(
                            type=AGUIEventType.TEXT_MESSAGE_START,
                            message_id=message_id,
                            role="assistant",
                        )
                    )
                    await merged.put(
                        TextMessageContentEvent(
                            type=AGUIEventType.TEXT_MESSAGE_CONTENT,
                            message_id=message_id,
                            delta=text,
                        )
                    )
                    await merged.put(
                        TextMessageEndEvent(
                            type=AGUIEventType.TEXT_MESSAGE_END, message_id=message_id
                        )
                    )

            await merged.put(
                RunFinishedEvent(
                    type=AGUIEventType.RUN_FINISHED, thread_id=thread_id, run_id=run_id
                )
            )
        finally:
            # Park this thread's interrupt state (if still paused) and leave a
            # fresh, inert state on the orchestrator so no other thread's run
            # can observe this conversation's gates.
            parked = bridge.swap_interrupt_state(self.orchestrator)
            if parked is not None and getattr(parked, "activated", False):
                self._parked_interrupts[thread_id] = parked
            else:
                self._parked_interrupts.pop(thread_id, None)


# ── helpers ──────────────────────────────────────────────────────────────────


def _extract_user_task(input_data: RunAgentInput) -> str:
    """Return the latest user message text as the orchestration task."""
    for msg in reversed(input_data.messages or []):
        if getattr(msg, "role", None) == "user":
            content = getattr(msg, "content", None)
            if isinstance(content, str):
                return content
            return str(content or "")
    return ""


def _build_resume_responses(
    orchestrator: MultiAgentBase, resume_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map AG-UI resume entries onto the orchestrator's pending interrupts.

    Produces ``[{"interruptResponse": {"interruptId", "response"}}, ...]`` which
    is a valid ``MultiAgentInput`` — ``stream_async`` feeds it to
    ``_interrupt_state.resume(task)``.
    """
    lookup: dict[str, Any] = {}
    for entry in resume_entries:
        interrupt_id = entry.get("interruptId", "")
        status = entry.get("status", "resolved")
        payload = entry.get("payload")
        if status == "resolved":
            lookup[interrupt_id] = payload if payload is not None else {"status": "approved"}
        else:
            lookup[interrupt_id] = {"status": "cancelled"}

    responses: list[dict[str, Any]] = []
    for intr_id in bridge.pending_interrupts(orchestrator, unresolved_only=True):
        if intr_id in lookup:
            user_resp = lookup[intr_id]
        else:
            logger.warning("resume did not address interrupt %s; defaulting to cancelled", intr_id)
            user_resp = {"status": "cancelled"}
        responses.append({"interruptResponse": {"interruptId": intr_id, "response": user_resp}})
    return responses


def _history_snapshot(
    input_data: RunAgentInput, exchange: HistoryExchange
) -> dict[str, Any] | None:
    """Build a STATE_SNAPSHOT payload carrying this run's ``kaboo_history``.

    Returns ``None`` when there is nothing to persist so we don't emit an empty
    snapshot on turns without sub-agent history.
    """
    merged_history = {**exchange.inbound, **exchange.outbound}
    if not merged_history:
        return None
    base = dict(input_data.state) if isinstance(input_data.state, dict) else {}
    base["kaboo_history"] = merged_history
    return base


def _result_text(result: Any, node_id: str | None) -> str:
    """Extract assistant text from a MultiAgentResult for *node_id* (or any node).

    Falls back to concatenating the final node's agent-result text when the
    specific node isn't found — keeps a reply on the chat even for dynamic
    swarms where the terminal node isn't known statically.
    """
    results = getattr(result, "results", None)
    if not isinstance(results, dict) or not results:
        return ""
    candidates = []
    if node_id and node_id in results:
        candidates = [results[node_id]]
    else:
        candidates = list(results.values())
    texts: list[str] = []
    for node_result in candidates:
        getter = getattr(node_result, "get_agent_results", None)
        agent_results = getter() if callable(getter) else []
        for agent_result in agent_results:
            texts.append(_agent_result_text(agent_result))
    return "".join(t for t in texts if t)


def _agent_result_text(agent_result: Any) -> str:
    """Pull the concatenated text blocks out of a strands ``AgentResult``."""
    message = getattr(agent_result, "message", None)
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return ""
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "".join(parts)
