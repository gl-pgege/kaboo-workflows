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
import copy
import inspect
import logging
import mimetypes
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ag_ui.core import (
    AgentCapabilities,
    HumanInTheLoopCapabilities,
    IdentityCapabilities,
    MultiAgentCapabilities,
    RunAgentInput,
    RunErrorEvent,
    RunStartedEvent,
    StateCapabilities,
    ToolCallResultEvent,
    ToolsCapabilities,
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
    StateChannel,
    set_activity_context,
    set_auth_context,
    set_forwarded_props,
    set_history_exchange,
    set_references,
    set_session_exchange,
    set_state_channel,
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
from kaboo_workflows.hooks import (
    ForwardedPropsHook,
    SessionStateHook,
    restore_session_state,
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
from kaboo_workflows.wire import EventQueue

from ..hooks.state_effects import compile_effects, fold_effects
from . import _strands_bridge as bridge
from ._a2ui_actions import take_a2ui_action
from ._client_tools import (
    entry_agent_def,
    equip_entry_agent,
    prepare_run,
    take_browser_answers,
    waiting,
)
from ._interrupts import browser_resume_entries
from ._sessions import inbound_sessions, sessions_delta
from ._subagents import RunEvents, ToolOutcomes, split_subagent_messages

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    AuthVerifier = Callable[[Request], Principal | None | Awaitable[Principal | None]]
    """User-supplied inbound auth verifier.

    Receives the FastAPI :class:`~fastapi.Request` and returns a
    :class:`~kaboo_workflows._context.Principal` (or ``None`` for an anonymous
    caller). May be sync or async. Raise (e.g.
    :class:`fastapi.HTTPException`) to reject the request.
    """

logger = logging.getLogger(__name__)


class _SuppressInterruptRawWarning(logging.Filter):
    """Drop ag-ui-strands' warning that a strands interrupt event is not RAW-safe.

    Every approval and question is a strands interrupt, which this adapter maps
    to the run's interrupt outcome itself. ag-ui-strands also offers the event
    for RAW forwarding, fails to serialize it, and would warn on each one.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return "tool_interrupt_event" not in record.getMessage()


logging.getLogger("ag_ui_strands.agent").addFilter(_SuppressInterruptRawWarning())

_DONE = bridge.STREAM_DONE


class _RawSSE:
    __slots__ = ("payload",)

    def __init__(self, payload: str) -> None:
        self.payload = payload


def _is_run_finished(event: Any) -> bool:
    return getattr(event, "type", None) == AGUIEventType.RUN_FINISHED


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


async def _consume_run(
    agui_agent: StrandsAgent,
    input_data: RunAgentInput,
    merged: asyncio.Queue[Any],
    exchange: HistoryExchange,
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

    Ends the run by writing the conversation's sessions back as a
    ``STATE_DELTA`` (see :mod:`~kaboo_workflows.adapters._sessions`).
    """
    unresolved_tool_calls: dict[str, str] = {}
    try:
        async for agui_event in agui_agent.run(input_data):
            etype = getattr(agui_event, "type", None)
            if etype == AGUIEventType.TOOL_CALL_START:
                unresolved_tool_calls[agui_event.tool_call_id] = agui_event.tool_call_name
            elif etype == AGUIEventType.TOOL_CALL_RESULT:
                tc_id = getattr(agui_event, "tool_call_id", None)
                if tc_id:
                    unresolved_tool_calls.pop(tc_id, None)
            elif etype == AGUIEventType.STATE_SNAPSHOT and isinstance(agui_event.snapshot, dict):
                fold_effects(agui_event.snapshot)

            if _is_run_finished(agui_event):
                await exchange.settle()
                if (delta := sessions_delta(exchange)) is not None:
                    await merged.put(delta)
                parked = bridge.get_thread_agent(agui_agent, input_data.thread_id)
                # A run waiting on a browser tool is paused too, with nothing to
                # report but the call itself.
                paused = getattr(getattr(agui_event, "outcome", None), "type", None) == (
                    "interrupt"
                ) or (parked is not None and bridge.is_interrupt_active(parked))
                # A paused (interrupt) run resumes and closes its own calls, so
                # leave them open. A truly-finished run is terminal — no result
                # can arrive after RUN_FINISHED — so close anything strands left
                # open (e.g. an internal tool error) to avoid a dangling toolUse.
                if not paused and unresolved_tool_calls:
                    for tc_id in list(unresolved_tool_calls):
                        await merged.put(_terminal_result_event(tc_id, _ERROR_RESULT))
                    unresolved_tool_calls.clear()
            await merged.put(agui_event)
    except Exception as exc:
        logger.error("AG-UI agent run failed: %s", exc, exc_info=True)
        await exchange.settle(0)
        for tc_id in list(unresolved_tool_calls):
            await merged.put(_terminal_result_event(tc_id, _ERROR_RESULT))
        unresolved_tool_calls.clear()
        await merged.put(
            RunErrorEvent(type=AGUIEventType.RUN_ERROR, message=str(exc), code="AGENT_ERROR")
        )
    finally:
        await merged.put(_DONE)


async def _sse_response(
    consume: Any,
    encoder: EventEncoder,
    merged: asyncio.Queue[Any],
    cleanup: Callable[[], Awaitable[None]] | None = None,
    run_events: RunEvents | None = None,
) -> AsyncIterator[str]:
    """Stream the run's AG-UI events, encoded, as the run produces them.

    ``run_events`` adds what the response itself contributes: the sub-agents'
    events, which reach *merged* as they happen, and the closing of every
    sub-agent before the run ends (see
    :class:`~kaboo_workflows.adapters._subagents.RunEvents`).

    ``cleanup`` runs once the stream is finished, cancelled or errored — the last
    moment the run owns anything. A per-run session's MCP clients are released
    here, which is why they cannot go stale between runs.
    """
    run_task = asyncio.create_task(consume())
    try:
        while True:
            item = await merged.get()
            if item is _DONE:
                break
            for event in run_events.emit(item) if run_events is not None else [item]:
                try:
                    frame = event.payload if isinstance(event, _RawSSE) else encoder.encode(event)
                except Exception:
                    logger.debug("failed to encode event: %s", type(event).__name__, exc_info=True)
                    continue
                yield frame
    finally:
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
    path: str,
    *,
    auth: AuthVerifier | None = None,
) -> None:
    """Register the AG-UI endpoint, resolving the session serving each request.

    ``resolve_session`` returns the :class:`AguiSession` for a request — one built
    at boot when the process serves a single fixed config, or one built for this
    run from the config it submitted.
    """

    @app.post(path)
    async def kaboo_endpoint(input_data: RunAgentInput, request: Request) -> StreamingResponse:
        # ag_ui's EventEncoder(accept: str = None) is mis-stubbed (default None
        # but annotated str); passing the optional Accept header through is correct.
        encoder = EventEncoder(accept=request.headers.get("accept"))  # ty: ignore[invalid-argument-type]
        thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
        take_a2ui_action(input_data)
        carried = split_subagent_messages(input_data)
        browser_answers = take_browser_answers(input_data, carried)
        resuming = bool(input_data.resume or browser_answers)
        turn_id = _resolve_turn_id(thread_id, input_data.run_id, is_resume=resuming)
        set_activity_context(thread_id, input_data.run_id, turn_id)

        # Inbound auth: validate/extract the caller identity and bind it to the
        # request context BEFORE any run task or MCP client starts, so outbound
        # auth strategies (relay) and per-request MCP clients inherit it
        # via contextvars.copy_context(). A raise here rejects the request.
        await _verify_inbound(auth, request)

        # The conversation's sessions (see _sessions): every agent's transcript
        # and the paused checkpoint, from the state the run carries. Agents seed
        # from them and capture back into them, and the run writes them back as
        # a STATE_DELTA. Set before any task is created so the run task inherits
        # the same exchanges.
        history, paused = inbound_sessions(input_data.state)
        exchange = HistoryExchange(inbound=history)
        set_history_exchange(exchange)
        set_session_exchange(SessionExchange(inbound=paused, resuming=resuming))

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

        # Build the session last, so anything it starts — notably MCP clients,
        # whose relay strategy snapshots contextvars at start — sees the
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
        browser_tools = prepare_run(input_data, session)

        merged: asyncio.Queue[Any] = asyncio.Queue()
        run_events = RunEvents(
            merged, carried, subagents=not session.orchestrated, browser_tools=browser_tools
        )
        session.event_queue.flush()
        session.event_queue.forward_to(run_events.sink)
        open_state_channel(input_data, session, run_events)

        async def release() -> None:
            """Stop forwarding events; release the session if this run owns it."""
            session.event_queue.forward_to(None)
            if session.transient:
                await session.aclose()

        # A Swarm or Graph entry runs on ag-ui-strands' own orchestrator bridge,
        # which handles its interrupts and resumes itself.
        if session.orchestrated:

            async def consume_orchestrated() -> None:
                await _consume_run(agui_agent, input_data, merged, exchange)

            return StreamingResponse(
                _sse_response(
                    consume_orchestrated, encoder, merged, release, run_events=run_events
                ),
                media_type=encoder.get_content_type(),
            )

        if resuming and not await _prepare_resume(agui_agent, input_data, browser_answers):
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

        await equip_entry_agent(input_data, session)

        async def consume() -> None:
            await _consume_run(agui_agent, input_data, merged, exchange)

        return StreamingResponse(
            _sse_response(consume, encoder, merged, release, run_events=run_events),
            media_type=encoder.get_content_type(),
        )


def open_state_channel(
    input_data: RunAgentInput, session: AguiSession, run_events: RunEvents
) -> None:
    """Bind the run's shared state and its state effects, before the run task starts."""
    app_config = session.resolved.app_config
    set_state_channel(
        StateChannel(
            state=copy.deepcopy(input_data.state) if isinstance(input_data.state, dict) else {},
            effects=compile_effects(app_config.state_effects) if app_config is not None else [],
            emit=run_events.delta,
        )
    )


async def _prepare_resume(
    agui_agent: StrandsAgent, input_data: RunAgentInput, browser_answers: dict[str, Any]
) -> bool:
    """Ready the paused agent for ag-ui-strands' native resume.

    A process that never ran the thread — it restarted, or another replica
    served the turn that paused — seeds the thread's agent and restores the
    paused state the client sent, because ag-ui-strands checks a resume against
    the agent's own checkpoint. The browser's answers to delegated agents'
    browser tools join the resume as entries for the interrupts carrying them.

    Returns:
        ``False`` when there is no paused agent to resume.
    """
    thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
    strands_agent = bridge.get_thread_agent(agui_agent, thread_id)
    cold = strands_agent is None
    if cold:
        try:
            strands_agent = await bridge.ensure_thread_agent(agui_agent, input_data)
        except Exception:
            logger.error("could not build an agent for thread_id=%s", thread_id, exc_info=True)
            return False
        bridge.seed_history(strands_agent, input_data)
    if not restore_session_state(strands_agent) and cold:
        return False
    if browser_answers:
        input_data.resume = [
            *(input_data.resume or []),
            *browser_resume_entries(bridge.pending_interrupts(strands_agent), browser_answers),
        ]
    return True


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

    agui_agent: StrandsAgent
    entry_name: str
    event_queue: EventQueue
    resolved: ResolvedConfig
    #: Whether the entry is a Swarm or Graph, which ag-ui-strands' orchestrator
    #: bridge drives and streams itself.
    orchestrated: bool = False
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
    infra, wires their event queue, and wraps the entry in ag-ui-strands'
    :class:`StrandsAgent`, which drives an agent and, through its orchestrator
    bridge, a Swarm or Graph.

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

    orchestrated = isinstance(entry, MultiAgentBase)
    agui_agent: StrandsAgent
    if orchestrated:
        agui_agent = StrandsAgent(
            agent=cast(Any, entry), name=entry_name, config=_build_agui_config(resolved)
        )
    elif isinstance(entry, Agent):
        # Plain agent or delegate (a forked Agent) — ag-ui-strands drives it.
        # Forward the entry node's kaboo hook providers so interrupt/HITL hooks
        # fire on the per-thread clone ag-ui-strands actually executes (the
        # clone does not inherit the blueprint's HookRegistry).
        # ag-ui-strands streams the entry itself, so its publisher's group is the
        # chat's, never a sub-agent's.
        entry_publisher = getattr(entry, "_kaboo_event_publisher", None)
        if entry_publisher is not None:
            entry_publisher.mark_inline_chat_owner()
        forwarded_hooks = get_agent_hook_providers(entry)

        # Every per-thread clone gets the request's forwarded props in its
        # state; per-invocation prompt/model overrides are opt-in via the root
        # runtime.allow_invocation_overrides config flag.
        runtime_def = getattr(app_config, "runtime", None)
        forwarded_hooks = [
            ForwardedPropsHook(
                apply_agent_config=bool(getattr(runtime_def, "allow_invocation_overrides", False))
            ),
            ToolOutcomes(),
            *forwarded_hooks,
        ]

        # Durable interrupts without a store, on by default. Goes on the clone
        # because that is the agent whose _interrupt_state holds the pending gate.
        if bool(getattr(runtime_def, "persist_session_state", True)):
            forwarded_hooks = [SessionStateHook(), *forwarded_hooks]

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
        orchestrated=orchestrated,
        mcp_clients=mcp_clients,
        transient=transient,
    )


class _ConversationSessions:
    """A fixed-config process's sessions, one per conversation.

    Agents hold a conversation's working state, among it a delegate's messages
    and the question or approval it is paused on, so two conversations must
    never run on the same objects. A thread gets its own session the first time
    it runs and keeps it, so a pause resumes warm. Beyond ``limit`` the least
    recently used are dropped; a dropped conversation resumes cold, as it would
    after a restart, from what its turns carry.
    """

    def __init__(self, app_config: AppConfig, infra: ResolvedInfra, *, limit: int = 256) -> None:
        self._app_config = app_config
        self._infra = infra
        self._limit = limit
        self._sessions: OrderedDict[str, AguiSession] = OrderedDict()

    def __call__(self, input_data: RunAgentInput) -> AguiSession:
        thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
        session = self._sessions.get(thread_id)
        if session is None:
            session = _build_session(self._app_config, self._infra, session_id=thread_id)
            self._sessions[thread_id] = session
            while len(self._sessions) > self._limit:
                self._sessions.popitem(last=False)
        else:
            self._sessions.move_to_end(thread_id)
        return session

    def kept(self) -> list[AguiSession]:
        """The sessions still held, for closing their queues at shutdown."""
        return list(self._sessions.values())

    def forget(self, thread_id: str) -> None:
        """Drop a conversation's session; it resumes cold if it runs again."""
        self._sessions.pop(thread_id, None)


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

    By default the process serves one config: it is loaded and checked here, and
    each conversation gets its own agents the first time it runs, keeping them
    for its later turns, so no two conversations share a sub-agent's state.

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

    resolve_session: Callable[[RunAgentInput], AguiSession]
    static_session: AguiSession | None = None
    conversations: _ConversationSessions | None = None
    if session_config_key is None:
        # One config for the process: build it here so a broken config fails at
        # startup rather than on the first request. Each conversation then runs
        # on its own session.
        static_session = _build_session(app_config, infra)
        resolve_session = conversations = _ConversationSessions(app_config, infra)
    else:
        resolve_session = _make_session_resolver(
            base_raw,
            infra,
            session_config_key=session_config_key,
            allowed_mcp_hosts=allowed_mcp_hosts,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        logger.info("AG-UI server starting — MCP lifecycle already active")
        try:
            yield
        finally:
            logger.info("AG-UI server shutting down — stopping MCP lifecycle")
            if static_session is not None:
                await static_session.event_queue.close()
            for session in conversations.kept() if conversations is not None else []:
                await session.event_queue.close()
            infra.mcp_lifecycle.stop()

    app = FastAPI(title="kaboo-workflows", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else ["*"],
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _add_kaboo_endpoint(app, resolve_session, endpoint, auth=auth)
    if ping_path is not None:
        add_ping(app, ping_path)

    @app.get("/manifest")
    async def manifest() -> dict:
        return {"entry": static_session.entry_name if static_session else app_config.entry}

    capabilities = agent_capabilities(app_config, per_run_configs=per_run_configs)

    @app.get(f"{endpoint.rstrip('/')}/capabilities")
    async def get_capabilities() -> dict:
        return capabilities.model_dump(by_alias=True, exclude_none=True)

    return app


def agent_capabilities(
    app_config: AppConfig, *, per_run_configs: bool = False
) -> AgentCapabilities:
    """What a service built from ``app_config`` can do, as AG-UI ``AgentCapabilities``.

    With per-run configs each run brings its own workflow, so the base config
    only says what every run can rely on and the rest is declared possible.
    """
    modes = {orchestration.mode for orchestration in app_config.orchestrations.values()}
    approvals = per_run_configs or any(
        agent.interrupt is not None
        and bool(agent.interrupt.tools or agent.interrupt.tool_approvals)
        for agent in app_config.agents.values()
    )
    return AgentCapabilities(
        identity=IdentityCapabilities(
            name=app_config.entry,
            type="kaboo-workflows",
            version=pkg_version("kaboo-workflows"),
        ),
        multi_agent=MultiAgentCapabilities(
            supported=per_run_configs or bool(modes),
            delegation=per_run_configs or "delegate" in modes,
            handoffs=per_run_configs or "swarm" in modes,
        ),
        human_in_the_loop=HumanInTheLoopCapabilities(
            supported=True, interrupts=True, approvals=approvals
        ),
        state=StateCapabilities(snapshots=True, deltas=True),
        tools=ToolsCapabilities(supported=True, client_provided=True),
        custom={"perRunWorkflows": per_run_configs},
    )


def _build_agui_config(resolved: Any) -> StrandsAgentConfig:
    """Build the ag-ui-strands config: browser tools that wait, sessions per thread, no snapshots.

    Every browser tool the entry agent may call waits for the browser's answer
    (see :mod:`~kaboo_workflows.adapters._client_tools`), so no call is ever
    answered with a placeholder.

    A conversation has exactly one identity: the AG-UI ``thread_id``. When the
    YAML declares a global ``session_manager:``, we hand ag-ui-strands a
    per-thread provider that resolves that def with ``session_id = thread_id``
    (the provider is called once per thread). There is no separately-minted
    server session id — the thread *is* the session.

    A run streams only what it adds. ``MESSAGES_SNAPSHOT``, which repeats the
    whole conversation at every step, is off: with it a response grows with the
    thread times the steps of the turn, and a long thread reaches the response
    cap. Clients build the transcript from the streamed events.
    """
    app_config = getattr(resolved, "app_config", None)
    entry_def = entry_agent_def(app_config) if app_config is not None else None
    behaviors = waiting(entry_def.client_tools) if entry_def is not None else {}
    session_def = getattr(app_config, "session_manager", None) if app_config else None

    provider = None
    if session_def is not None:
        definition = session_def

        def provider(input_data: RunAgentInput) -> Any:
            thread_id = input_data.thread_id or bridge.DEFAULT_THREAD
            return resolve_session_manager(definition, session_id_override=thread_id)

    return StrandsAgentConfig(
        tool_behaviors=behaviors,
        session_manager_provider=provider,
        emit_messages_snapshot=False,
    )


def _resolve_entry_name(entry: Any, resolved: Any) -> str:
    """Determine the human-readable name for the entry node."""
    for name, agent in resolved.agents.items():
        if agent is entry:
            return name
    for name, orch in resolved.orchestrators.items():
        if orch is entry:
            return name
    return "entry"
