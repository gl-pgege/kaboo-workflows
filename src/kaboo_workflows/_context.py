"""Per-conversation activity context.

Carries the current ``thread_id`` / ``run_id`` through the async call stack so
that :class:`~kaboo_workflows.hooks.EventPublisher` can stamp every
:class:`~kaboo_workflows.types.StreamEvent` with the conversation it belongs to.

This is what allows a single server process to serve multiple concurrent
conversations without their activity streams clobbering one another. On
AgentCore (one microVM per session) it is redundant but harmless; on a plain
self-hosted process it is what makes per-conversation routing correct.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

_current_thread_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kaboo_thread_id", default=None
)
_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kaboo_run_id", default=None
)
_current_turn_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kaboo_turn_id", default=None
)
_current_delegation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kaboo_delegation_id", default=None
)


@dataclass
class HistoryExchange:
    """Request-scoped carrier for client-driven, per-agent history.

    ``inbound`` holds the transcripts the client sent this run (parsed from
    ``RunAgentInput.state['kaboo_history']``), keyed per agent bucket.
    ``outbound`` accumulates the transcripts agents produce this run; it is
    merged back into the outgoing ``STATE_SNAPSHOT`` so the client persists
    and replays them next turn.

    One instance is created per request and shared by reference across the
    request's tasks via :data:`_current_history`, so a sub-agent hook running
    in a child task and the snapshot enricher running in the parent see the
    same buffers.
    """

    inbound: dict[str, list[Any]] = field(default_factory=dict)
    outbound: dict[str, list[Any]] = field(default_factory=dict)


_current_history: contextvars.ContextVar[HistoryExchange | None] = contextvars.ContextVar(
    "kaboo_history_exchange", default=None
)


@dataclass
class Reference:
    """A single client-supplied reference for the current run.

    A reference is a *pointer* the model can cite and resolve on demand — a file
    attachment or a custom entity (table, dashboard, …). It is never an eager
    snapshot; resolution happens at agent runtime via a tool.

    Attributes:
        kind: Semantic type of the reference (``"file"``, ``"table"``, …). File
            attachments use ``"file"`` (or a MIME-derived modality); custom
            entities use whatever kind the frontend minted.
        id: Stable id minted by the frontend (or a deterministic server
            fallback). This is the key the manifest lists and a resolver tool
            looks up, so it is identical on both sides of the wire.
        name: Human-readable label (filename or entity name) shown in the
            manifest.
        transport: ``"attachment"`` for blob-capable file references (delivered
            as message parts, resolvable to bytes) or ``"object"`` for
            pointer-only custom entities (resolved by a user MCP tool).
        mime_type: MIME type for attachments (``None`` for objects).
        source: For attachments, ``"url"`` or ``"data"`` describing where the
            bytes live; ``None`` for objects.
        value: For attachments, the URL or base64 payload matching ``source``;
            ``None`` for objects.
        meta: Arbitrary extra metadata carried from the frontend (object
            references only).
    """

    kind: str
    id: str
    name: str
    transport: str = "attachment"
    mime_type: str | None = None
    source: str | None = None
    value: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


_current_references: contextvars.ContextVar[list[Reference] | None] = contextvars.ContextVar(
    "kaboo_references", default=None
)

# Reference ids the model asked to inline via ``fetch_attachment``. This is a
# shared, mutable set bound once per request (like :class:`HistoryExchange`) so
# a tool running in a worker thread and the ReferenceHook running on the agent
# task mutate/read the *same* object. Media (images, PDFs, …) can't be returned
# in a tool-role message on OpenAI-compatible providers, so the tool records the
# id here and the hook materializes it into a user message instead.
_current_inline_requests: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "kaboo_inline_requests", default=None
)


def set_activity_context(
    thread_id: str | None,
    run_id: str | None = None,
    turn_id: str | None = None,
) -> None:
    """Bind the current conversation's ``thread_id`` / ``run_id`` / ``turn_id``.

    Call at the start of each request handler. Tasks created afterwards
    (via ``asyncio.create_task``) inherit a copy of this context, so nested
    agent/delegate activity is attributed to the right conversation.

    ``turn_id`` identifies one logical user turn and is **stable across an
    interrupt/resume** (unlike ``run_id``, which changes on the resume POST).
    It lets the UI bind every group a turn produced — even ones that first
    appear only after a resume — to that turn's single chat reply.
    """
    _current_thread_id.set(thread_id)
    _current_run_id.set(run_id)
    _current_turn_id.set(turn_id)


def get_thread_id() -> str | None:
    """Return the current conversation's ``thread_id`` (or ``None``)."""
    return _current_thread_id.get()


def get_run_id() -> str | None:
    """Return the current conversation's ``run_id`` (or ``None``)."""
    return _current_run_id.get()


def get_turn_id() -> str | None:
    """Return the current logical turn's id (stable across resume, or ``None``)."""
    return _current_turn_id.get()


@contextlib.contextmanager
def delegation_context(tool_use_id: str | None) -> Iterator[None]:
    """Bind the delegating tool-call id for the wrapped node invocation.

    A coordinator delegates to a sub-agent by calling a tool; that tool call has
    a ``toolUseId`` which is the same id the frontend (AG-UI / CopilotKit) sees
    for the inline card. Binding it here lets
    :class:`~kaboo_workflows.hooks.EventPublisher` stamp the sub-agent's stream
    group with that id, so the card can be correlated to its activity group by a
    stable key instead of by arrival order.

    Nesting is token-based: a sub-agent that itself delegates temporarily
    overrides the id and restores the parent's on exit.
    """
    token = _current_delegation_id.set(tool_use_id)
    try:
        yield
    finally:
        _current_delegation_id.reset(token)


def get_delegation_id() -> str | None:
    """Return the current delegating tool-call id (or ``None``)."""
    return _current_delegation_id.get()


def set_history_exchange(exchange: HistoryExchange | None) -> None:
    """Bind the current request's :class:`HistoryExchange`.

    Call at the start of each request handler, before creating the run task.
    Tasks created afterwards inherit a copy of this context that references
    the same exchange object, so hook writes made in child tasks are visible
    to the parent snapshot enricher.
    """
    _current_history.set(exchange)


def get_history_exchange() -> HistoryExchange | None:
    """Return the current request's :class:`HistoryExchange` (or ``None``)."""
    return _current_history.get()


def set_references(references: list[Reference] | None) -> None:
    """Bind the current request's parsed :class:`Reference` list.

    Call at the start of each request handler, before creating the run task, so
    tasks created afterwards inherit a copy of this context. The
    :class:`~kaboo_workflows.hooks.ReferenceHook` and the built-in reference
    tools read it to build the manifest and resolve pointers.
    """
    _current_references.set(references)


def get_references() -> list[Reference]:
    """Return the current request's references (empty list when none)."""
    return _current_references.get() or []


def set_inline_requests(requests: set[str] | None) -> None:
    """Bind the current request's on-demand inline-media request set.

    Call at the start of each request handler, before creating the run task, so
    every task inherits the *same* mutable set. ``fetch_attachment`` adds a
    reference id here when the model asks to read a media attachment; the
    :class:`~kaboo_workflows.hooks.ReferenceHook` then materializes it into a
    user message before the next model call.
    """
    _current_inline_requests.set(requests)


def get_inline_requests() -> set[str]:
    """Return the current request's inline-media request set (empty when none)."""
    return _current_inline_requests.get() or set()


def request_inline(reference_id: str) -> None:
    """Record that the model asked to inline the given attachment reference."""
    requests = _current_inline_requests.get()
    if requests is not None:
        requests.add(reference_id)
