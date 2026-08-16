"""AG-UI server — expose YAML-defined agents as AG-UI SSE endpoints.

Bridges kaboo-workflows (YAML -> strands.Agent) with ag-ui-strands
(strands.Agent -> AG-UI SSE). This is the primary serving mechanism
for kaboo-workflows, producing CopilotKit-compatible event streams.

Hierarchical sub-agent activity rides the same run stream: it is emitted as
AG-UI ``ACTIVITY_SNAPSHOT`` events interleaved on the ``/invocations`` endpoint
(scoped per ``thread_id``), so a single canonical stream carries everything and
frontends read it via the AG-UI agent subscription — there is no separate
activity SSE endpoint.

Usage::

    from kaboo_workflows.adapters import create_agui_app

    app = create_agui_app("config.yaml")
    # Run with: uvicorn module:app --port 8080
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import mimetypes
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

from ag_ui.core import (
    ActivitySnapshotEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunStartedEvent,
    ToolCallResultEvent,
    ToolMessage,
)
from ag_ui.core import EventType as AGUIEventType
from ag_ui.encoder import EventEncoder
from ag_ui_strands import StrandsAgent, StrandsAgentConfig
from ag_ui_strands.endpoint import add_ping
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from strands import Agent
from strands.multiagent.base import MultiAgentBase

from kaboo_workflows._context import (
    HistoryExchange,
    Principal,
    Reference,
    SessionExchange,
    set_activity_context,
    set_auth_context,
    set_forwarded_props,
    set_history_exchange,
    set_inline_requests,
    set_references,
    set_session_exchange,
)
from kaboo_workflows.config import (
    AppConfig,
    ResolvedConfig,
    ResolvedInfra,
    load_session,
    load_session_config,
    parse_config_sources,
    resolve_infra,
    resolve_run_clients,
    validate_raw_config,
)
from kaboo_workflows.config.resolvers import resolve_session_manager
from kaboo_workflows.config.resolvers.agents import get_agent_hook_providers
from kaboo_workflows.config.resolvers.config import _resolve_chat_output, _resolve_chat_owner
from kaboo_workflows.hooks import (
    ForwardedPropsHook,
    SessionStateHook,
    restore_session_state,
    session_state_snapshot,
)
from kaboo_workflows.mcp import MCPLifecycle
from kaboo_workflows.telemetry import init_telemetry
from kaboo_workflows.tools.fetching import (
    ConfiguredReferenceFetcher,
    get_attachment_url_template,
    install_agui_strands_fetch,
    set_attachment_url_template,
    set_reference_fetcher,
)
from kaboo_workflows.types import StreamEvent
from kaboo_workflows.wire import EventQueue

from . import _strands_bridge as bridge
from ._activity import ActivityRegistry
from ._interrupts import map_strands_interrupt_to_agui as _map_strands_interrupt_to_agui
from ._multiagent import StrandsMultiAgent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    AuthVerifier = Callable[[Request], Principal | None | Awaitable[Principal | None]]
    """User-supplied inbound auth verifier.

    Receives the FastAPI :class:`~fastapi.Request` and returns a
    :class:`~kaboo_workflows._context.Principal` (or ``None`` for an anonymous
    caller). May be sync or async. Raise (e.g.
    :class:`fastapi.HTTPException`) to reject the request.
    """

logger = logging.getLogger(__name__)

_DONE = bridge.STREAM_DONE


class _RawSSE:
    __slots__ = ("payload",)

    def __init__(self, payload: str) -> None:
        self.payload = payload


def _is_run_finished(event: Any) -> bool:
    return getattr(event, "type", None) == AGUIEventType.RUN_FINISHED


def _maybe_inject_interrupt_outcome(
    event: Any,
    agui_agent: StrandsAgent,
    thread_id: str | None,
) -> Any:
    """Replace a plain RUN_FINISHED with an interrupt outcome when one is pending.

    ag-ui-strands has no concept of interrupts, so it emits a normal
    ``RunFinishedEvent`` even when the underlying strands agent paused. We
    detect the pending (unresolved) interrupts and re-frame the outcome so the
    frontend can render an approval / input request.
    """
    strands_agent = bridge.get_thread_agent(agui_agent, thread_id)
    if strands_agent is None or not bridge.is_interrupt_active(strands_agent):
        return event

    pending = bridge.pending_interrupts(strands_agent, unresolved_only=True)
    if not pending:
        return event

    agui_interrupts = [_map_strands_interrupt_to_agui(intr) for intr in pending.values()]
    logger.debug("injecting interrupt outcome: %s", [i["id"] for i in agui_interrupts])
    return RunFinishedEvent(
        type=AGUIEventType.RUN_FINISHED,
        thread_id=event.thread_id,
        run_id=event.run_id,
        outcome=RunFinishedInterruptOutcome.model_validate({"interrupts": agui_interrupts}),
    )


def _build_resume_responses(
    strands_agent: Any, resume_entries: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Map AG-UI resume entries onto the coordinator's pending interrupts."""
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
    # Only the still-unresolved interrupts need a response; strands keeps
    # already-resolved ones in its state and does not expect them re-addressed.
    for intr_id in bridge.pending_interrupts(strands_agent, unresolved_only=True):
        if intr_id in lookup:
            user_resp = lookup[intr_id]
        else:
            # No resume entry addressed this interrupt. Never silently approve —
            # default to a safe rejection so an unhandled approval can't execute.
            logger.warning("resume did not address interrupt %s; defaulting to cancelled", intr_id)
            user_resp = {"status": "cancelled"}
        responses.append({"interruptResponse": {"interruptId": intr_id, "response": user_resp}})
    return responses


_ABANDONED_RESULT = "[interrupted: superseded by a new user message]"
_ERROR_RESULT = "[tool call did not complete: run error]"


def _terminal_result_event(tc_id: str, content: str) -> ToolCallResultEvent:
    """Build a terminal ToolCallResult that closes an open tool call.

    Used on every non-happy-path termination (run error, superseded interrupt)
    so kaboo never leaves a ``toolUse`` without a matching ``toolResult`` in the
    client transcript it produces.
    """
    return ToolCallResultEvent(
        type=AGUIEventType.TOOL_CALL_RESULT,
        tool_call_id=tc_id,
        message_id=str(uuid.uuid4()),
        content=content,
        role="tool",
    )


def _find_transcript_imbalance(messages: list[Any] | None) -> str | None:
    """Return a description of a malformed tool-block transcript, else ``None``.

    ag-ui-strands rebuilds the coordinator's history 1:1 from this transcript
    (``_build_strands_history`` -> ``stream_async(None)``) with no validation. A
    ``toolUse`` without a matching ``toolResult`` (dangling) or more than one
    ``toolResult`` for the same ``toolUse`` (duplicate) yields a native history
    the model rejects, which manifests as a silent, event-less hang. We detect
    those two conditions so the caller can fail loudly instead.
    """
    open_calls: dict[str, str] = {}
    result_counts: dict[str, int] = {}
    for msg in messages or []:
        role = getattr(msg, "role", None)
        if role == "assistant":
            for tc in getattr(msg, "tool_calls", None) or []:
                open_calls[tc.id] = _tool_call_name(tc)
        elif role == "tool":
            tcid = getattr(msg, "tool_call_id", None)
            if tcid:
                result_counts[tcid] = result_counts.get(tcid, 0) + 1
    dangling = {cid: n for cid, n in open_calls.items() if cid not in result_counts}
    duplicates = {cid: c for cid, c in result_counts.items() if c > 1}
    problems: list[str] = []
    if dangling:
        problems.append(f"tool calls missing a result: {dangling}")
    if duplicates:
        problems.append(f"tool calls with duplicate results: {duplicates}")
    return "; ".join(problems) if problems else None


def _tool_call_name(tc: Any) -> str:
    fn = getattr(tc, "function", None)
    if isinstance(fn, dict):
        return fn.get("name") or "?"
    return getattr(fn, "name", None) or "?"


def _close_abandoned_tool_calls(input_data: RunAgentInput) -> list[str]:
    """Balance a transcript whose interrupt was superseded by a new user turn.

    When the user sends a fresh message while a prior interrupt is still pending,
    the paused tool call is genuinely abandoned. We insert a terminal
    ``toolResult`` immediately after the assistant ``toolUse`` that lacks one so
    the current turn's rebuilt history is well-formed, and return the closed ids
    so the caller can also emit the result to the frontend (keeping its persisted
    transcript balanced for subsequent turns).
    """
    messages = list(input_data.messages or [])
    result_ids = {
        getattr(m, "tool_call_id", None) for m in messages if getattr(m, "role", None) == "tool"
    }
    closed: list[str] = []
    repaired: list[Any] = []
    for msg in messages:
        repaired.append(msg)
        if getattr(msg, "role", None) != "assistant":
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            if tc.id not in result_ids:
                repaired.append(
                    ToolMessage(
                        id=str(uuid.uuid4()),
                        role="tool",
                        content=_ABANDONED_RESULT,
                        tool_call_id=tc.id,
                    )
                )
                closed.append(tc.id)
    if closed:
        input_data.messages = repaired
    return closed


def _collapse_duplicate_tool_results(input_data: RunAgentInput) -> dict[str, int]:
    """Keep only the last ``tool`` result per tool-call id in the transcript.

    A single tool call that raises **more than one** interrupt produces duplicate
    results in the client transcript. When a nested sub-agent asks the user and
    then requests tool approval, both interrupts bubble up keyed to the *outer*
    delegate tool call (see :func:`_tool_call_id_from_interrupt_id`); the client
    (CopilotKit) records each interrupt *resolution* as a ``tool`` message for
    that id, plus the delegate's final genuine result. Replaying that 1:1 into
    strands yields one ``toolUse`` with N ``toolResult``s — native history the
    model rejects (the same failure mode :func:`_find_transcript_imbalance`
    guards against).

    Those intermediate resolutions are client-side HITL bookkeeping, not real
    tool outputs; the last result is the genuine one. Dropping the earlier
    duplicates makes the rebuilt history well-formed while preserving meaning
    (the coordinator only ever needed one result for its delegate call). Returns
    a ``{tool_call_id: dropped_count}`` map (empty when nothing was collapsed).
    """
    messages = list(input_data.messages or [])
    counts: dict[str, int] = {}
    for msg in messages:
        if getattr(msg, "role", None) == "tool":
            tcid = getattr(msg, "tool_call_id", None)
            if tcid:
                counts[tcid] = counts.get(tcid, 0) + 1
    dupes = {tcid for tcid, c in counts.items() if c > 1}
    if not dupes:
        return {}

    last_index: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if getattr(msg, "role", None) == "tool":
            tcid = getattr(msg, "tool_call_id", None)
            if tcid in dupes:
                last_index[tcid] = i

    kept: list[Any] = []
    dropped: dict[str, int] = {}
    for i, msg in enumerate(messages):
        tcid = getattr(msg, "tool_call_id", None)
        if getattr(msg, "role", None) == "tool" and tcid in dupes and i != last_index[tcid]:
            dropped[tcid] = dropped.get(tcid, 0) + 1
            continue
        kept.append(msg)
    input_data.messages = kept
    return dropped


class TranscriptRepairs(NamedTuple):
    """Outcome of :func:`normalize_client_transcript`.

    ``backfill_ids``  tool calls closed as abandoned — the caller re-emits their
                      terminal result to the frontend so its store stays balanced.
    ``collapsed``     ``{tool_call_id: dropped_count}`` for duplicate results removed.
    ``residual``      a description of any imbalance left *after* repair, or
                      ``None``. Non-``None`` means genuinely foreign/unrecoverable
                      data the caller should refuse loudly.
    """

    backfill_ids: list[str]
    collapsed: dict[str, int]
    residual: str | None


def normalize_client_transcript(
    input_data: RunAgentInput, *, close_dangling: bool
) -> TranscriptRepairs:
    """Turn an arbitrary client transcript into valid strands native history.

    This is the single boundary for the fresh-run path, which replays the client
    (CopilotKit) transcript 1:1 into strands via ``stream_async(None)``. strands
    requires every ``toolUse`` to have exactly one ``toolResult``; the client can
    violate that in two ways, each repaired by a focused helper:

    - **Abandoned calls** (:func:`_close_abandoned_tool_calls`): the user sent a
      new message past a pending interrupt, leaving a ``toolUse`` with no result.
      Closed **only** when ``close_dangling`` (the supersede case); the returned
      ids let the caller also balance the frontend store. A dangling call in any
      other context is treated as foreign and left for ``residual`` to refuse —
      we surface it loudly rather than guess.
    - **Duplicate results** (:func:`_collapse_duplicate_tool_results`): a tool
      call that raised multiple interrupts has each resolution recorded by the
      client as a result (plus the one genuine result); collapsed to the last.

    Any imbalance remaining after repair is reported as ``residual`` (normally
    ``None``) so the caller can fail fast instead of hanging the model. New client
    HITL quirks get one obvious home here instead of another ad-hoc guard.
    """
    backfill_ids = _close_abandoned_tool_calls(input_data) if close_dangling else []
    collapsed = _collapse_duplicate_tool_results(input_data)
    residual = _find_transcript_imbalance(input_data.messages)
    return TranscriptRepairs(backfill_ids=backfill_ids, collapsed=collapsed, residual=residual)


def _parse_resume_entries(input_data: RunAgentInput) -> list[dict[str, Any]] | None:
    raw_resume = input_data.resume
    if not raw_resume:
        return None
    return [
        {
            "interruptId": entry.interrupt_id,
            "status": entry.status,
            "payload": entry.payload,
        }
        for entry in raw_resume
    ]


_ATTACHMENT_PART_TYPES = ("image", "audio", "video", "document")


def _latest_user_message(input_data: RunAgentInput) -> Any | None:
    """Return the most recent ``user`` message, or ``None``."""
    for msg in reversed(input_data.messages or []):
        if getattr(msg, "role", None) == "user":
            return msg
    return None


def _parse_attachment_parts(message: Any) -> list[Reference]:
    """Extract attachment references from a user message's content parts.

    Only multimodal parts (image/audio/video/document) become references; a
    plain-text message yields none. The reference id is read from the part's
    ``metadata`` (``kaboo_id``, minted by the frontend) so the manifest, the
    resolver tool, and the model all agree on the same key; a part without one
    gets a deterministic fallback (``<message id>:<index>``) stable within the
    message.
    """
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return []
    msg_id = getattr(message, "id", None) or "ref"
    refs: list[Reference] = []
    for i, part in enumerate(content):
        ptype = getattr(part, "type", None)
        if ptype not in _ATTACHMENT_PART_TYPES:
            continue
        source = getattr(part, "source", None)
        meta = getattr(part, "metadata", None)
        meta = meta if isinstance(meta, dict) else {}
        kind = meta.get("kaboo_kind") or ptype
        ref_id = meta.get("kaboo_id") or f"{msg_id}:{i}"
        name = meta.get("kaboo_name") or meta.get("filename") or f"{kind}-{i}"
        refs.append(
            Reference(
                kind=str(kind),
                id=str(ref_id),
                name=str(name),
                transport="attachment",
                mime_type=getattr(source, "mime_type", None),
                source=getattr(source, "type", None),
                value=getattr(source, "value", None),
                meta={k: v for k, v in meta.items() if not str(k).startswith("kaboo_")},
            )
        )
    return refs


def _parse_object_references(input_data: RunAgentInput) -> list[Reference]:
    """Extract object references from ``state.kaboo_references``.

    Custom entities (tables, dashboards, …) are cited by the frontend and travel
    in AG-UI state rather than on the message. Each entry supplies ``kind``,
    ``id`` and ``name``; ``meta`` is passed through for the user's resolver tool.

    Entries of kind ``"attachment"`` are *files*, not custom entities: when a
    content URL is available (``meta.url``, or synthesized from the configured
    ``attachments.content_url_template``), they are upgraded to attachment
    transport so ``fetch_attachment`` works on every turn — not only the first,
    where the host also sends multimodal message parts.
    """
    state = input_data.state
    if not isinstance(state, dict):
        return []
    raw = state.get("kaboo_references")
    if not isinstance(raw, list):
        return []
    refs: list[Reference] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ref_id = entry.get("id")
        if not ref_id:
            continue
        meta = entry.get("meta")
        ref = Reference(
            kind=str(entry.get("kind") or "object"),
            id=str(ref_id),
            name=str(entry.get("name") or ref_id),
            transport="object",
            meta=meta if isinstance(meta, dict) else {},
        )
        if ref.kind == "attachment":
            _upgrade_attachment_reference(ref)
        refs.append(ref)
    return refs


def _upgrade_attachment_reference(ref: Reference) -> None:
    """Give an attachment-kind object reference a fetchable transport in place.

    The URL comes from ``meta.url`` when the host supplied one, else from the
    configured content URL template. Without either, the reference stays a
    plain object (there is nothing to fetch).
    """
    url = ref.meta.get("url")
    if not (isinstance(url, str) and url):
        template = get_attachment_url_template()
        if template is None:
            return
        url = template.format(id=ref.id)
    ref.transport = "attachment"
    ref.source = "url"
    ref.value = url
    mime = ref.meta.get("mimeType") or ref.meta.get("mime_type")
    if not (isinstance(mime, str) and mime):
        mime = mimetypes.guess_type(ref.name)[0]
    if mime:
        ref.mime_type = mime


def _parse_references(input_data: RunAgentInput) -> list[Reference]:
    """Build the request's full reference registry from message parts + state.

    Combines blob-capable attachment references (multimodal message parts) with
    pointer-only object references (``state.kaboo_references``) into one list,
    deduped by id (message parts win on collision).
    """
    refs: list[Reference] = []
    message = _latest_user_message(input_data)
    if message is not None:
        refs.extend(_parse_attachment_parts(message))
    seen = {r.id for r in refs}
    for ref in _parse_object_references(input_data):
        if ref.id not in seen:
            refs.append(ref)
            seen.add(ref.id)
    return refs


def _enrich_history_snapshot(agui_event: Any, exchange: HistoryExchange) -> None:
    """Merge this run's sub-agent transcripts into a STATE_SNAPSHOT in place.

    ag-ui-strands echoes ``input_data.state`` back in its state snapshots but
    never updates ``kaboo_history`` — that is kaboo's client-driven per-agent
    history. We fold the captured ``outbound`` transcripts over the client's
    ``inbound`` so the (authoritative) final snapshot carries the up-to-date
    history for the frontend to persist and replay next turn.
    """
    snapshot = getattr(agui_event, "snapshot", None)
    if not isinstance(snapshot, dict):
        return
    merged_history = {**exchange.inbound, **exchange.outbound}
    if merged_history:
        snapshot["kaboo_history"] = merged_history
        logger.debug(
            "kaboo_history write-back | keys=%s captured=%s",
            list(merged_history),
            {k: len(v) for k, v in exchange.outbound.items()},
        )


def _enrich_session_snapshot(agui_event: Any) -> None:
    """Write the executing agent's session state into a STATE_SNAPSHOT in place.

    The counterpart to :func:`_enrich_history_snapshot`, for the state that used
    to be lost on restart: a paused approval. The host persists the snapshot and
    replays it, so the next turn can resume a gate this process never saw.

    Read at snapshot time rather than from a hook because an interrupt pauses the
    run — the state we want is what the agent holds after it has stopped.
    """
    snapshot = getattr(agui_event, "snapshot", None)
    if not isinstance(snapshot, dict):
        return
    state = session_state_snapshot()
    if state is None:
        return
    snapshot["kaboo_session"] = state
    logger.debug("kaboo_session write-back | %s", state.get("interrupt_state", {}).get("activated"))


def _parse_session_state(input_data: RunAgentInput) -> dict[str, Any]:
    """Read the client's ``kaboo_session`` state for this run."""
    if not isinstance(input_data.state, dict):
        return {}
    raw = input_data.state.get("kaboo_session")
    return raw if isinstance(raw, dict) else {}


def _restore_snapshot_user_content(agui_event: Any, originals: dict[str, Any]) -> None:
    """Put structured multimodal user content back on a MESSAGES_SNAPSHOT.

    ag-ui-strands seeds each ``MessagesSnapshotEvent`` from the input messages
    but coerces every user ``content`` to text (``str(content)``), which turns a
    multimodal message (text + attachment parts) into a Python repr string. The
    frontend keys off these snapshots, so it would render that repr instead of
    the file card. We swap the original parts (kept by message id) back in so the
    snapshot the frontend persists matches what it sent.
    """
    for msg in getattr(agui_event, "messages", None) or []:
        if getattr(msg, "role", None) != "user":
            continue
        original = originals.get(getattr(msg, "id", None))
        if original is not None:
            msg.content = original


async def _consume_run(
    agui_agent: StrandsAgent,
    input_data: RunAgentInput,
    merged: asyncio.Queue[Any],
    exchange: HistoryExchange,
    *,
    backfill_tool_calls: list[str] | None = None,
) -> None:
    """Drive one ag-ui-strands run and forward its AG-UI events onto *merged*.

    Shared by the fresh-run and resume paths. Guarantees each tool call gets
    exactly one terminal ``toolResult`` in the produced transcript:

    - An interrupt outcome means the run **paused**, not terminated. Every open
      tool call will resume and emit its own real result, so we must NOT close
      them here — doing so used to emit a second, duplicate ``toolResult`` once
      the call actually completed, which poisoned the next turn's replayed
      history and hung ``stream_async(None)``.
    - On a genuine failure we close every still-open call before the error so no
      ``toolUse`` is left dangling.

    ``backfill_tool_calls`` are ids for calls abandoned by a superseded
    interrupt; their terminal results are emitted right after RUN_STARTED so the
    frontend's persisted transcript stays balanced for subsequent turns. Also
    enriches STATE_SNAPSHOT events with this run's client-driven sub-agent
    history (``kaboo_history``).
    """
    unresolved_tool_calls: dict[str, str] = {}
    pending_backfill = list(backfill_tool_calls or [])
    # Original multimodal user content, keyed by message id. ag-ui-strands
    # rebuilds MESSAGES_SNAPSHOT with a stringified (`str(content)`) view of the
    # user message, which would clobber the frontend's structured attachment
    # parts with a Python repr. We restore the real parts on the way out.
    original_user_content = {
        mid: content
        for m in (getattr(input_data, "messages", None) or [])
        if getattr(m, "role", None) == "user"
        and isinstance((content := getattr(m, "content", None)), list)
        and (mid := getattr(m, "id", None))
    }
    try:
        async for agui_event in agui_agent.run(input_data):
            etype = getattr(agui_event, "type", None)
            if etype == AGUIEventType.TOOL_CALL_START:
                unresolved_tool_calls[agui_event.tool_call_id] = agui_event.tool_call_name
            elif etype == AGUIEventType.TOOL_CALL_RESULT:
                tc_id = getattr(agui_event, "tool_call_id", None)
                if tc_id:
                    unresolved_tool_calls.pop(tc_id, None)
            elif etype == AGUIEventType.STATE_SNAPSHOT:
                _enrich_history_snapshot(agui_event, exchange)
                _enrich_session_snapshot(agui_event)
            elif etype == AGUIEventType.MESSAGES_SNAPSHOT and original_user_content:
                _restore_snapshot_user_content(agui_event, original_user_content)

            if _is_run_finished(agui_event):
                replaced = _maybe_inject_interrupt_outcome(
                    agui_event, agui_agent, input_data.thread_id
                )
                paused = replaced is not agui_event
                # A paused (interrupt) run resumes and closes its own calls, so
                # leave them open. A truly-finished run is terminal — no result
                # can arrive after RUN_FINISHED — so close anything strands left
                # open (e.g. an internal tool error) to avoid a dangling toolUse.
                if not paused and unresolved_tool_calls:
                    for tc_id in list(unresolved_tool_calls):
                        await merged.put(_terminal_result_event(tc_id, _ERROR_RESULT))
                    unresolved_tool_calls.clear()
                agui_event = replaced
            await merged.put(agui_event)

            # Backfill abandoned calls immediately after the run has started so
            # the frontend closes them in its store (ordering after RUN_STARTED
            # keeps the AG-UI protocol valid).
            if pending_backfill and etype == AGUIEventType.RUN_STARTED:
                for tc_id in pending_backfill:
                    await merged.put(_terminal_result_event(tc_id, _ABANDONED_RESULT))
                pending_backfill = []
    except Exception as exc:
        logger.error("AG-UI agent run failed: %s", exc, exc_info=True)
        for tc_id in list(unresolved_tool_calls):
            await merged.put(_terminal_result_event(tc_id, _ERROR_RESULT))
        unresolved_tool_calls.clear()
        await merged.put(
            RunErrorEvent(type=AGUIEventType.RUN_ERROR, message=str(exc), code="AGENT_ERROR")
        )
    finally:
        await merged.put(_DONE)


def _make_activity_pump(
    event_queue: EventQueue,
    registry: ActivityRegistry,
    entry_name: str,
    merged: asyncio.Queue[Any],
    *,
    entry_inline: bool = False,
) -> Any:
    """Build a task coroutine that folds queue events into the registry.

    Events are routed by the ``thread_id`` stamped on them (via contextvars) so
    the correct conversation's activity is updated regardless of which concurrent
    request task drained the shared queue. Whenever a folded event changes the
    state, a fresh ``ACTIVITY_SNAPSHOT`` is put onto *merged* so it interleaves
    live on the run's ``/invocations`` stream.

    The entry node's events are excluded from group rendering by default
    because its text is the chat reply (rendered by the host) and re-surfacing
    it would duplicate the answer — only its ``AGENT_COMPLETE`` token usage is
    folded into the per-run rollup so run totals include the entry agent.
    A plain-agent entry (``entry_inline``) is the exception: its events ARE
    routed so its own tool calls enrich the inline tool rows — its group carries
    ``inline_chat_owner`` so the UI never draws a duplicate card for it.
    """

    def fold(event: Any) -> ActivitySnapshotEvent | None:
        if not isinstance(event, StreamEvent):
            return None
        thread_id = event.data.get("thread_id") or bridge.DEFAULT_THREAD
        if event.agent_name == entry_name and not entry_inline:
            # The entry/manager agent's stream is not rendered as a group,
            # but its completions still count toward the run's token total.
            changed = registry.apply_usage(thread_id, event)
        else:
            changed = registry.apply(thread_id, event)
        if not changed:
            return None
        return ActivitySnapshotEvent(
            message_id=f"kaboo.activity.{thread_id}",
            activity_type="kaboo.activity",
            content=registry.snapshot(thread_id),
            replace=True,
        )

    async def pump_activity() -> None:
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=0.15)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            if event is None:
                break
            snapshot = fold(event)
            if snapshot is not None:
                await merged.put(snapshot)

    def flush(thread_id: str) -> list[ActivitySnapshotEvent]:
        """Drain the queue and return one final snapshot for *thread_id*.

        Called when the run stream ends. The entry agent's final AGENT_COMPLETE
        (carrying the run's token usage) lands on the queue just before
        RUN_FINISHED — and even when the pump task dequeues it in time, the
        snapshot it puts on *merged* can land after the stream loop has already
        broken. Folding what is left and re-snapshotting the registry is
        deterministic either way: the final snapshot reflects everything folded,
        no matter which side got to the event first.
        """
        while True:
            event = event_queue.get_nowait()
            if event is None:
                break
            fold(event)
        content = registry.snapshot(thread_id)
        if not content.get("groups") and not content.get("usageByRun"):
            return []
        return [
            ActivitySnapshotEvent(
                message_id=f"kaboo.activity.{thread_id}",
                activity_type="kaboo.activity",
                content=content,
                replace=True,
            )
        ]

    return pump_activity, flush


async def _sse_response(
    consume: Any,
    activity_pump: Any,
    encoder: EventEncoder,
    merged: asyncio.Queue[Any],
    cleanup: Callable[[], Awaitable[None]] | None = None,
    activity_flush: Callable[[], list[Any]] | None = None,
) -> AsyncIterator[str]:
    """Stream encoded AG-UI events; run the run + activity pumps concurrently.

    ``activity_flush`` drains the not-yet-pumped activity events when the run
    stream ends, so the final snapshot (e.g. the entry agent's token usage,
    emitted just before RUN_FINISHED) is delivered instead of being lost to
    the pump-cancellation race.

    ``cleanup`` runs once the stream is finished, cancelled or errored — the last
    moment the run owns anything. A per-run session's MCP clients are released
    here, which is why they cannot go stale between runs.
    """
    run_task = asyncio.create_task(consume())
    activity_task = asyncio.create_task(activity_pump())
    try:
        while True:
            item = await merged.get()
            if item is _DONE:
                break
            terminal = getattr(item, "type", None) in (
                AGUIEventType.RUN_FINISHED,
                AGUIEventType.RUN_ERROR,
            )
            if terminal and activity_flush is not None:
                # The terminal event closes the run for AG-UI clients, so any
                # still-queued activity (the entry agent's final completion
                # with the run's token usage) must be delivered first.
                for snapshot in activity_flush():
                    try:
                        yield encoder.encode(snapshot)
                    except Exception:
                        logger.debug("failed to encode flushed snapshot", exc_info=True)
            try:
                if isinstance(item, _RawSSE):
                    yield item.payload
                else:
                    yield encoder.encode(item)
            except Exception:
                logger.debug("failed to encode event: %s", type(item).__name__, exc_info=True)
    finally:
        activity_task.cancel()
        try:
            await activity_task
        except asyncio.CancelledError:
            pass
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
        if cleanup is not None:
            try:
                await cleanup()
            except Exception:
                logger.warning("failed to release run resources", exc_info=True)


# Per-thread current turn id. A "turn" is one user message and everything it
# produces, including work that only appears after an interrupt/resume. The
# resume POST carries a fresh run_id, so run_id can't identify the turn; we mint
# a turn id on each non-resume POST and reuse it on resumes of the same thread.
_turn_by_thread: dict[str, str] = {}


def _resolve_turn_id(thread_id: str, run_id: str | None, *, is_resume: bool) -> str:
    """Return the stable turn id for this request (see :data:`_turn_by_thread`)."""
    if is_resume:
        existing = _turn_by_thread.get(thread_id)
        if existing is not None:
            return existing
    turn_id = run_id or str(uuid.uuid4())
    _turn_by_thread[thread_id] = turn_id
    return turn_id


async def _verify_inbound(auth: AuthVerifier | None, request: Request) -> Principal | None:
    """Run the user-supplied inbound verifier (sync or async) and bind identity.

    Returns the resolved :class:`Principal` (``None`` when no verifier is
    configured). The verifier may raise to reject the request; the exception
    propagates to FastAPI unchanged (e.g. ``HTTPException`` -> 401).
    """
    if auth is None:
        set_auth_context(None)
        return None
    result = auth(request)
    if inspect.isawaitable(result):
        result = await result
    principal = cast("Principal | None", result)
    set_auth_context(principal)
    return principal


def _add_kaboo_endpoint(
    app: FastAPI,
    resolve_session: Callable[[RunAgentInput], AguiSession],
    registry: ActivityRegistry,
    path: str,
    *,
    auth: AuthVerifier | None = None,
) -> None:
    """Register the AG-UI endpoint, resolving the session serving each request.

    ``resolve_session`` returns the :class:`AguiSession` for a request — one built
    at boot when the process serves a single fixed config, or one built for this
    run from the config it submitted. Only the activity registry spans requests.
    """

    @app.post(path)
    async def kaboo_endpoint(input_data: RunAgentInput, request: Request) -> StreamingResponse:
        # ag_ui's EventEncoder(accept: str = None) is mis-stubbed (default None
        # but annotated str); passing the optional Accept header through is correct.
        encoder = EventEncoder(accept=request.headers.get("accept"))  # ty: ignore[invalid-argument-type]
        thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
        resume_entries = _parse_resume_entries(input_data)
        turn_id = _resolve_turn_id(
            thread_id, input_data.run_id, is_resume=resume_entries is not None
        )
        set_activity_context(thread_id, input_data.run_id, turn_id)

        # Inbound auth: validate/extract the caller identity and bind it to the
        # request context BEFORE any run task or MCP client starts, so outbound
        # auth strategies (relay / OBO) and per-request MCP clients inherit it
        # via contextvars.copy_context(). A raise here rejects the request.
        await _verify_inbound(auth, request)

        # Client-driven per-agent history. Sub-agents seed their transcripts
        # from this exchange (via HistoryHook) and capture back into it; the
        # merged result is folded into the outgoing STATE_SNAPSHOT. Set before
        # any task is created so the run task inherits the same exchange object.
        inbound_history: dict[str, Any] = {}
        if isinstance(input_data.state, dict):
            raw_history = input_data.state.get("kaboo_history")
            if isinstance(raw_history, dict):
                inbound_history = raw_history
        exchange = HistoryExchange(inbound=inbound_history)
        set_history_exchange(exchange)

        # Client-driven session state, on the same channel and for the same
        # reason: it lets a paused approval outlive this process. SessionStateHook
        # restores it onto the executing agent; _enrich_session_snapshot writes
        # back what the run leaves behind.
        set_session_exchange(SessionExchange(inbound=_parse_session_state(input_data)))

        # Client-supplied references (file attachments on the message + custom
        # entities in state.kaboo_references). Parsed once per request and bound
        # to the context so the per-agent ReferenceHook and built-in reference
        # tools can inject the manifest / resolve pointers. Set before any task
        # is created so the run task inherits the same list.
        set_references(_parse_references(input_data))
        # The host's per-run side channel (AG-UI forwardedProps): run-scoped
        # credentials, per-run agent config, tenant context. Bound before any
        # task is created so host tools/hooks and built-in features read the
        # right run's props via get_forwarded_props().
        set_forwarded_props(
            input_data.forwarded_props if isinstance(input_data.forwarded_props, dict) else {}
        )
        # On-demand media the model fetches via fetch_attachment is materialized
        # into a user message (not a tool result) so it works across providers.
        # Bind the shared set here so the run task and its tool threads share it.
        set_inline_requests(set())

        # Build the session last, so anything it starts — notably MCP clients,
        # whose relay/OBO strategies snapshot contextvars at start — sees the
        # caller's identity and this run's props.
        try:
            session = resolve_session(input_data)
        except Exception as exc:
            logger.error("could not build a session for thread_id=%s", thread_id, exc_info=True)
            reason = str(exc)

            async def config_error_gen() -> AsyncIterator[str]:
                yield encoder.encode(
                    RunStartedEvent(
                        type=AGUIEventType.RUN_STARTED,
                        thread_id=thread_id,
                        run_id=input_data.run_id,
                    )
                )
                yield encoder.encode(
                    RunErrorEvent(
                        type=AGUIEventType.RUN_ERROR,
                        message=f"Invalid workflow config: {reason}",
                        code="INVALID_CONFIG",
                    )
                )

            return StreamingResponse(config_error_gen(), media_type=encoder.get_content_type())

        agui_agent = session.agui_agent
        entry_name = session.entry_name

        async def release() -> None:
            """Release the session if this run owns it (see :class:`AguiSession`)."""
            if session.transient:
                await session.aclose()

        merged: asyncio.Queue[Any] = asyncio.Queue()
        activity_pump, _activity_flush = _make_activity_pump(
            session.event_queue, registry, entry_name, merged, entry_inline=session.entry_inline
        )

        def activity_flush() -> list[Any]:
            return _activity_flush(thread_id)

        # First-class Swarm/Graph entry: kaboo owns the run loop (ag-ui-strands
        # cannot drive a MultiAgentBase). No chat-side tool blocks are produced,
        # so the transcript-imbalance / abandoned-tool-call machinery below does
        # not apply — a superseded interrupt is simply cleared.
        if isinstance(agui_agent, StrandsMultiAgent):
            if not resume_entries and agui_agent.is_interrupt_active(thread_id):
                last_role = input_data.messages[-1].role if input_data.messages else None
                if last_role == "user":
                    logger.debug("multiagent: new user turn with stale interrupt; clearing")
                    agui_agent.deactivate_interrupts(thread_id)
                else:
                    pending = agui_agent.pending_interrupts(thread_id, unresolved_only=True)
                    if pending:
                        agui_interrupts = [
                            _map_strands_interrupt_to_agui(intr) for intr in pending.values()
                        ]
                        run_id = input_data.run_id or str(uuid.uuid4())

                        async def replay_multiagent_interrupt() -> AsyncIterator[str]:
                            yield encoder.encode(
                                RunStartedEvent(
                                    type=AGUIEventType.RUN_STARTED,
                                    thread_id=thread_id,
                                    run_id=run_id,
                                )
                            )
                            yield encoder.encode(
                                RunFinishedEvent(
                                    type=AGUIEventType.RUN_FINISHED,
                                    thread_id=thread_id,
                                    run_id=run_id,
                                    outcome=RunFinishedInterruptOutcome.model_validate(
                                        {"interrupts": agui_interrupts}
                                    ),
                                )
                            )

                        await release()
                        return StreamingResponse(
                            replay_multiagent_interrupt(),
                            media_type=encoder.get_content_type(),
                        )

            async def consume_multiagent() -> None:
                await agui_agent.consume(
                    input_data, merged, exchange, resume_entries=resume_entries
                )

            return StreamingResponse(
                _sse_response(
                    consume_multiagent,
                    activity_pump,
                    encoder,
                    merged,
                    release,
                    activity_flush=activity_flush,
                ),
                media_type=encoder.get_content_type(),
            )

        if resume_entries:
            strands_agent = bridge.get_thread_agent(agui_agent, thread_id)
            cold = strands_agent is None
            if cold:
                # This process has never run the thread — it restarted, or another
                # replica served the turn that paused. The client still holds the
                # gate on the state channel, so seed an agent and restore it rather
                # than failing an approval over a lost in-memory clone.
                try:
                    strands_agent = await bridge.ensure_thread_agent(agui_agent, input_data)
                except Exception as exc:
                    logger.error(
                        "could not build an agent for thread_id=%s during resume: %s",
                        thread_id,
                        exc,
                        exc_info=True,
                    )
                    strands_agent = None
            # Needed before the responses are built, because they are derived from
            # the agent's pending interrupts. Idempotent: the hook calls it too.
            restored = strands_agent is not None and restore_session_state(strands_agent)
            if strands_agent is None or (cold and not restored):
                logger.error("no resumable agent state for thread_id=%s", thread_id)

                async def error_gen() -> AsyncIterator[str]:
                    yield encoder.encode(
                        RunStartedEvent(
                            type=AGUIEventType.RUN_STARTED,
                            thread_id=thread_id,
                            run_id=input_data.run_id,
                        )
                    )
                    yield encoder.encode(
                        RunErrorEvent(
                            type=AGUIEventType.RUN_ERROR,
                            message="No agent session found for resume",
                            code="RESUME_NO_SESSION",
                        )
                    )

                await release()
                return StreamingResponse(error_gen(), media_type=encoder.get_content_type())

            responses = _build_resume_responses(strands_agent, resume_entries)
            logger.debug("resume thread=%s responses=%d", thread_id, len(responses))

            async def consume_resume() -> None:
                with bridge.resume_prompt_override(strands_agent, responses):
                    await _consume_run(agui_agent, input_data, merged, exchange)

            return StreamingResponse(
                _sse_response(
                    consume_resume,
                    activity_pump,
                    encoder,
                    merged,
                    release,
                    activity_flush=activity_flush,
                ),
                media_type=encoder.get_content_type(),
            )

        # Fresh (non-resume) run. Activity accumulates across the whole
        # conversation and is deliberately not cleared per turn, so an agent's Nth
        # group stays addressable as index N-1 by the frontend's monotonic cards.
        superseded = False
        existing_agent = bridge.get_thread_agent(agui_agent, thread_id)
        if existing_agent is not None and bridge.is_interrupt_active(existing_agent):
            last_role = input_data.messages[-1].role if input_data.messages else None
            if last_role == "user":
                # A new user turn arrived while a prior interrupt is still
                # pending (e.g. the user typed instead of resolving, or a prior
                # run was abandoned). Drop the stale interrupt and process the
                # new message — otherwise the conversation would be wedged in
                # interrupt mode and follow-ups would never run. The paused tool
                # call is now abandoned; the normalizer below closes it (in both
                # this turn's history and the frontend store) so nothing dangles.
                logger.debug("new user turn with stale interrupt; clearing it")
                bridge.deactivate_interrupts(existing_agent)
                superseded = True
            else:
                # Pure reconnect while genuinely paused (no new user input):
                # re-emit the pending interrupt so the frontend can re-render it.
                pending = bridge.pending_interrupts(existing_agent, unresolved_only=True)
                if pending:
                    agui_interrupts = [
                        _map_strands_interrupt_to_agui(intr) for intr in pending.values()
                    ]
                    run_id = input_data.run_id or str(uuid.uuid4())

                    async def replay_interrupt() -> AsyncIterator[str]:
                        yield encoder.encode(
                            RunStartedEvent(
                                type=AGUIEventType.RUN_STARTED, thread_id=thread_id, run_id=run_id
                            )
                        )
                        yield encoder.encode(
                            RunFinishedEvent(
                                type=AGUIEventType.RUN_FINISHED,
                                thread_id=thread_id,
                                run_id=run_id,
                                outcome=RunFinishedInterruptOutcome.model_validate(
                                    {"interrupts": agui_interrupts}
                                ),
                            )
                        )

                    await release()
                    return StreamingResponse(
                        replay_interrupt(), media_type=encoder.get_content_type()
                    )

        # Single boundary: turn the arbitrary client transcript into valid strands
        # native history for the 1:1 replay below (stream_async(None)). Closes
        # calls abandoned by a superseded interrupt (and reports their ids so we
        # rebalance the frontend store), collapses HITL interrupt-resolution
        # duplicates, and surfaces any residual (foreign/unrecoverable) imbalance.
        repairs = normalize_client_transcript(input_data, close_dangling=superseded)
        if repairs.backfill_ids:
            logger.debug("closed abandoned tool calls on supersede: %s", repairs.backfill_ids)
        if repairs.collapsed:
            logger.debug("collapsed duplicate interrupt-resolution results: %s", repairs.collapsed)
        if repairs.residual is not None:
            logger.error("refusing malformed transcript: %s", repairs.residual)
            run_id = input_data.run_id or str(uuid.uuid4())

            async def malformed_gen() -> AsyncIterator[str]:
                yield encoder.encode(
                    RunStartedEvent(
                        type=AGUIEventType.RUN_STARTED, thread_id=thread_id, run_id=run_id
                    )
                )
                yield encoder.encode(
                    RunErrorEvent(
                        type=AGUIEventType.RUN_ERROR,
                        message=f"Malformed conversation history: {repairs.residual}",
                        code="MALFORMED_HISTORY",
                    )
                )

            await release()
            return StreamingResponse(malformed_gen(), media_type=encoder.get_content_type())

        async def consume_fresh() -> None:
            await _consume_run(
                agui_agent,
                input_data,
                merged,
                exchange,
                backfill_tool_calls=repairs.backfill_ids,
            )

        return StreamingResponse(
            _sse_response(
                consume_fresh,
                activity_pump,
                encoder,
                merged,
                release,
                activity_flush=activity_flush,
            ),
            media_type=encoder.get_content_type(),
        )


def _install_reference_fetching(app_config: Any) -> None:
    """Wire the config-driven reference fetching for this process.

    Builds a :class:`ConfiguredReferenceFetcher` when ``attachments.base_url``
    or ``attachments.authorization`` is set (a host-registered fetcher via
    ``set_reference_fetcher`` is respected and not overwritten), registers the
    ``content_url_template`` for object-reference upgrades, and routes
    ag-ui-strands' media fetching through the same funnel.
    """
    attachments = getattr(app_config, "attachments", None)
    base_url = getattr(attachments, "base_url", None)
    authorization = getattr(attachments, "authorization", None)
    from kaboo_workflows.tools.fetching import get_reference_fetcher

    if (base_url or authorization) and get_reference_fetcher() is None:
        set_reference_fetcher(
            ConfiguredReferenceFetcher(base_url=base_url, authorization=authorization)
        )
    set_attachment_url_template(getattr(attachments, "content_url_template", None))
    install_agui_strands_fetch()


@dataclass
class AguiSession:
    """Everything one conversation needs in order to run.

    Built by :func:`_build_session`. A session is cheap — resolving a config
    creates objects but opens no connections and starts no processes — which is
    what makes it reasonable to build one per run rather than once per process.

    Nothing here is shared between sessions except the models and MCP servers on
    the infra it was built from, so two runs of two different workflows cannot see
    each other's agents, hooks or activity stream.
    """

    agui_agent: StrandsAgent | StrandsMultiAgent
    entry_name: str
    event_queue: EventQueue
    resolved: ResolvedConfig
    entry_inline: bool = False
    #: The MCP client sessions opened for this session, owned so they can be
    #: closed with it. ``None`` when the clients belong to the process instead.
    mcp_clients: MCPLifecycle | None = None
    #: Whether this session belongs to a single run and is closed when it ends.
    #: ``False`` for the boot-built session a fixed-config process reuses.
    transient: bool = False

    async def aclose(self) -> None:
        """Release the session's resources: its event queue, then its MCP clients."""
        await self.event_queue.close()
        if self.mcp_clients is not None:
            self.mcp_clients.stop()


def _build_session(
    app_config: AppConfig,
    infra: ResolvedInfra,
    *,
    session_id: str | None = None,
    mcp_clients: MCPLifecycle | None = None,
    transient: bool = False,
) -> AguiSession:
    """Resolve a config into a runnable AG-UI session.

    Creates this session's own agents, orchestrations and entry from the shared
    infra, wires their activity event queue, and wraps the entry in the right
    AG-UI adapter — :class:`StrandsMultiAgent` for a swarm/graph entry, which
    kaboo drives itself, or :class:`StrandsAgent` for a plain agent, which
    ag-ui-strands drives.

    Args:
        app_config: The validated config for this session. May differ per run.
        infra: Shared models and MCP servers.
        session_id: Conversation id, threaded into per-agent session managers.
        mcp_clients: Client sessions this session owns and should close with it.
        transient: Whether the session is closed when its run ends.

    Returns:
        The session, ready to serve runs.

    Raises:
        TypeError: If the entry node is an unsupported orchestration type.
    """
    resolved = load_session(app_config, infra, session_id=session_id)
    entry = resolved.entry
    entry_name = _resolve_entry_name(entry, resolved)
    event_queue = resolved.wire_event_queue()

    entry_inline = False
    agui_agent: StrandsAgent | StrandsMultiAgent
    if isinstance(entry, MultiAgentBase):
        # First-class Swarm/Graph entry — kaboo owns the run loop.
        agui_agent = StrandsMultiAgent(
            entry, name=entry_name, chat_output=_resolve_chat_output(app_config)
        )
    elif isinstance(entry, Agent):
        # Plain agent or delegate (a forked Agent) — ag-ui-strands drives it.
        # Forward the entry node's kaboo hook providers so interrupt/HITL hooks
        # fire on the per-thread clone ag-ui-strands actually executes (the
        # clone does not inherit the blueprint's HookRegistry).
        forwarded_hooks = get_agent_hook_providers(entry)

        # Every per-thread clone gets the request's forwarded props in its
        # state; per-invocation prompt/model overrides are opt-in via the root
        # runtime.allow_invocation_overrides config flag.
        runtime_def = getattr(app_config, "runtime", None)
        forwarded_hooks = [
            ForwardedPropsHook(
                apply_agent_config=bool(getattr(runtime_def, "allow_invocation_overrides", False))
            ),
            *forwarded_hooks,
        ]

        # Durable interrupts without a store, on by default. Goes on the clone
        # because that is the agent whose _interrupt_state holds the pending gate.
        if bool(getattr(runtime_def, "persist_session_state", True)):
            forwarded_hooks = [SessionStateHook(), *forwarded_hooks]

        # A plain-agent entry (not a delegate) also forwards its EventPublisher so
        # its own tool calls land in the activity stream and enrich the inline tool
        # rows (labels, formatted results, error status) — otherwise those rows
        # render bare from the raw AG-UI message. The publisher goes FIRST so
        # TOOL_START is emitted before any HITL gate interrupts, registering the
        # tool in run 1 so its result updates on resume. Its group is flagged
        # inline_chat_owner so the UI enriches the rows but never draws a card.
        entry_pub = getattr(entry, "_kaboo_event_publisher", None)
        if entry_pub is not None:
            if _resolve_chat_owner(app_config) is None:
                entry_pub.mark_inline_chat_owner()
                entry_inline = True
            # Forwarded even for a delegate entry (entry_inline False): its
            # events stay excluded from group rendering, but AGENT_COMPLETE
            # carries the manager's token usage, which the activity pump folds
            # into the per-run rollup so run totals include the entry agent.
            forwarded_hooks = [entry_pub, *forwarded_hooks]

        agui_agent = StrandsAgent(
            agent=entry,
            name=entry_name,
            config=_build_agui_config(resolved),
            hooks=forwarded_hooks,
        )
    else:
        raise TypeError(f"Unsupported entry node type: {type(entry)}")

    return AguiSession(
        agui_agent=agui_agent,
        entry_name=entry_name,
        event_queue=event_queue,
        resolved=resolved,
        entry_inline=entry_inline,
        mcp_clients=mcp_clients,
        transient=transient,
    )


def _make_session_resolver(
    base_raw: dict,
    infra: ResolvedInfra,
    *,
    session_config_key: str,
    allowed_mcp_hosts: list[str] | None = None,
) -> Callable[[RunAgentInput], AguiSession]:
    """Build a resolver that gives each run the workflow it submitted.

    The submitted config is read from ``forwardedProps[session_config_key]`` and
    layered over ``base_raw``. The run's MCP clients are its own, so the returned
    session is transient and must be closed when the run ends.

    The config is re-read every turn, which is what lets an edited workflow take
    effect on the next message. Nothing is cached between runs: a session holds
    no conversation state — history and pending interrupts arrive with the turn —
    so rebuilding is indistinguishable from retaining, and a second replica or a
    restarted process behaves the same as this one.
    """

    def resolve_session(input_data: RunAgentInput) -> AguiSession:
        overlay = None
        props = input_data.forwarded_props
        if isinstance(props, dict):
            submitted = props.get(session_config_key)
            if isinstance(submitted, str) and submitted.strip():
                overlay = submitted
        run_config = load_session_config(base_raw, overlay, allowed_mcp_hosts=allowed_mcp_hosts)

        clients = resolve_run_clients(run_config, infra)
        # Started so the run owns the sessions and stop() actually closes them;
        # unpinned so they end with the run rather than the process.
        clients.start(pin_clients=False)
        return _build_session(
            run_config,
            replace(infra, clients=clients.clients, mcp_lifecycle=clients),
            session_id=input_data.thread_id or bridge.DEFAULT_THREAD,
            mcp_clients=clients,
            transient=True,
        )

    return resolve_session


def create_agui_app(
    config_path: str | Path,
    *,
    endpoint: str = "/invocations",
    ping_path: str | None = "/ping",
    cors_origins: list[str] | None = None,
    cors_allow_credentials: bool = True,
    auth: AuthVerifier | None = None,
    session_config_key: str | None = None,
    allowed_mcp_hosts: list[str] | None = None,
) -> FastAPI:
    """Create a FastAPI app serving AG-UI SSE from a YAML config.

    By default the process serves one config: it is loaded here, its agents are
    built once, and every run uses them.

    Setting ``session_config_key`` makes the service behave like a function
    instead. Each run submits its own config in ``forwardedProps`` under that key,
    layered over ``config_path`` as an overlay (see
    :func:`~kaboo_workflows.config.load_session_config`), and gets its own agents,
    orchestration, entry and MCP client sessions, all released when the run ends.
    Different runs can then be different workflows, and a restart or a second
    replica behaves like a cold instance because nothing is kept between runs.

    That is only safe because conversation state does not live in the objects
    being rebuilt: history and pending interrupts arrive with each turn on the
    AG-UI state channel (see
    :class:`~kaboo_workflows.hooks.SessionStateHook`).

    Args:
        config_path: Path to the kaboo-workflows YAML config. With
            ``session_config_key`` set this is the base every submitted config
            layers over — typically shared models, MCP clients and defaults.
        endpoint: Path for the AG-UI agent endpoint.
        ping_path: Path for the health check endpoint. ``None`` to disable.
        cors_origins: Allowed CORS origins. Defaults to ``["*"]``. Pass an
            explicit allow-list for production deployments.
        cors_allow_credentials: Whether to allow credentialed CORS requests.
        auth: Optional inbound auth verifier. Receives each ``/invocations``
            request and returns a
            :class:`~kaboo_workflows._context.Principal` (or ``None`` for an
            anonymous caller); raise to reject (e.g.
            :class:`fastapi.HTTPException` -> 401). The resolved identity is
            bound to the request context so outbound MCP auth strategies can
            relay/exchange from it. When ``None`` (default) the endpoint trusts
            its caller — only safe behind an authenticating proxy or the
            AgentCore Runtime authorizer.
        session_config_key: ``forwardedProps`` key carrying this run's config.
            ``None`` (default) serves ``config_path`` alone, unchanged.
        allowed_mcp_hosts: Hosts a submitted config's MCP client URL may point
            at. Set this whenever configs are authored anywhere but this
            repository: an arbitrary URL needs no code to exfiltrate.

    Returns:
        A FastAPI application with AG-UI SSE streaming.

    Raises:
        TypeError: If the entry node is an unsupported orchestration type.
    """
    config_path = Path(config_path).resolve()
    per_run_configs = session_config_key is not None

    # Parse the base once. With per-run configs it stays raw, because each run
    # validates its own merge of it; a run's overlay may legitimately replace
    # sections the base leaves incomplete.
    base_raw = parse_config_sources(str(config_path))
    app_config = validate_raw_config(base_raw)
    _install_reference_fetching(app_config)

    # Process-wide, idempotent, and a no-op when telemetry.enabled is false.
    # Initialized from the base config only — per-run overlays cannot toggle
    # the global tracer provider.
    init_telemetry(app_config.telemetry)

    infra = resolve_infra(app_config)
    # Servers are processes and stay process-wide either way. Clients are only
    # pinned for the process when the process owns them; per-run clients are
    # deliberately allowed to end with their run.
    infra.mcp_lifecycle.start(pin_clients=not per_run_configs)

    registry = ActivityRegistry()

    resolve_session: Callable[[RunAgentInput], AguiSession]
    static_session: AguiSession | None = None
    if session_config_key is None:
        # One config for the process: build it here so a broken config fails at
        # startup rather than on the first request.
        static_session = _build_session(app_config, infra)
        fixed = static_session

        def resolve_session(_input: RunAgentInput) -> AguiSession:
            return fixed
    else:
        resolve_session = _make_session_resolver(
            base_raw,
            infra,
            session_config_key=session_config_key,
            allowed_mcp_hosts=allowed_mcp_hosts,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("AG-UI server starting — MCP lifecycle already active")
        try:
            yield
        finally:
            logger.info("AG-UI server shutting down — stopping MCP lifecycle")
            if static_session is not None:
                await static_session.event_queue.close()
            infra.mcp_lifecycle.stop()

    app = FastAPI(title="kaboo-workflows", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else ["*"],
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _add_kaboo_endpoint(app, resolve_session, registry, endpoint, auth=auth)
    if ping_path is not None:
        add_ping(app, ping_path)

    @app.get("/manifest")
    async def manifest() -> dict:
        return {"entry": static_session.entry_name if static_session else app_config.entry}

    return app


def _build_agui_config(resolved: Any) -> StrandsAgentConfig | None:
    """Build the ag-ui-strands config, wiring session persistence to ``thread_id``.

    A conversation has exactly one identity: the AG-UI ``thread_id``. When the
    YAML declares a global ``session_manager:``, we hand ag-ui-strands a
    per-thread provider that resolves that def with ``session_id = thread_id``
    (the provider is called once per thread). There is no separately-minted
    server session id — the thread *is* the session.

    Returns ``None`` when no global session manager is configured (nothing to
    persist), preserving the in-memory per-thread behavior.
    """
    app_config = getattr(resolved, "app_config", None)
    session_def = getattr(app_config, "session_manager", None) if app_config else None
    if session_def is None:
        return None

    def _session_manager_provider(input_data: RunAgentInput) -> Any:
        thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
        return resolve_session_manager(session_def, session_id_override=thread_id)

    return StrandsAgentConfig(session_manager_provider=_session_manager_provider)


def _resolve_entry_name(entry: Any, resolved: Any) -> str:
    """Determine the human-readable name for the entry node."""
    for name, agent in resolved.agents.items():
        if agent is entry:
            return name
    for name, orch in resolved.orchestrators.items():
        if orch is entry:
            return name
    return "entry"
