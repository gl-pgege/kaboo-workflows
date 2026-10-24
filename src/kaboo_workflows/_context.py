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

import asyncio
import contextlib
import contextvars
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

SUMMARY_WAIT_SECONDS = 20.0
"""How long a run's write-back waits for memory summaries still being written."""

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
    ``RunAgentInput.state['kaboo_sessions']['history']``), keyed per agent bucket.
    ``outbound`` accumulates the transcripts agents produce this run, whole, so
    an agent called again in the same run is seeded with everything it did.
    ``persisted`` holds the copy to write back instead, for buckets whose agent
    keeps a masked history. :meth:`written_back` merges the three into the
    outgoing ``STATE_SNAPSHOT`` so the client persists and replays them next
    turn.

    One instance is created per request and shared by reference across the
    request's tasks via :data:`_current_history`, so a sub-agent hook running
    in a child task and the snapshot enricher running in the parent see the
    same buffers.

    ``summaries`` holds the summaries still being written, one per bucket. Until
    :meth:`settle` applies one, its bucket hands back the masked copy already in
    ``persisted``, so an agent returns to its caller without waiting for it.
    """

    inbound: dict[str, list[Any]] = field(default_factory=dict)
    outbound: dict[str, list[Any]] = field(default_factory=dict)
    persisted: dict[str, list[Any]] = field(default_factory=dict)
    summaries: dict[str, asyncio.Task[list[Any]]] = field(default_factory=dict)

    def written_back(self) -> dict[str, list[Any]]:
        """Return the history to hand back: this run's buckets over the client's.

        A bucket's ``persisted`` copy wins over its ``outbound`` transcript.
        """
        return {**self.inbound, **self.outbound, **self.persisted}

    def summarize_later(self, key: str, task: asyncio.Task[list[Any]]) -> None:
        """Let ``task`` replace ``persisted[key]`` once it is settled.

        A summary still pending for the bucket was written from an older copy,
        so it is cancelled.
        """
        previous = self.summaries.pop(key, None)
        if previous is not None:
            previous.cancel()
        self.summaries[key] = task

    async def settle(self, timeout: float = SUMMARY_WAIT_SECONDS) -> bool:
        """Wait up to ``timeout`` seconds for the pending summaries and apply them.

        One still running is cancelled and its bucket keeps the masked copy.
        Returns whether any bucket changed.
        """
        if not self.summaries:
            return False
        pending, self.summaries = self.summaries, {}
        done, running = await asyncio.wait(pending.values(), timeout=timeout)
        for task in running:
            task.cancel()
        changed = False
        for key, task in pending.items():
            if task not in done or task.cancelled() or task.exception() is not None:
                continue
            memory = task.result()
            if memory is not self.persisted.get(key):
                self.persisted[key] = memory
                changed = True
        return changed


_current_history: contextvars.ContextVar[HistoryExchange | None] = contextvars.ContextVar(
    "kaboo_history_exchange", default=None
)


@dataclass
class SessionExchange:
    """Request-scoped carrier for client-driven session state.

    ``inbound`` is the state the client sent this run (parsed from
    ``RunAgentInput.state['kaboo_sessions']['paused']``); a hook restores it onto the
    executing agent so a paused approval survives a process restart.

    ``agent`` is that executing agent, recorded on invocation so the snapshot
    enricher can serialize its state on the way out. We keep the agent rather
    than a copy of the state because an interrupt *pauses* a run: the state we
    need is whatever it holds once the run has stopped, which is later than any
    hook fires.

    ``publishes_approvals`` says whether this run writes the approvals it is
    waiting on to ``kaboo_sessions.approvals``, where a host reads them to match
    the person's decisions. Without it a host cannot remember a decision, so
    the tool gate offers only ``once``.

    ``restored`` records that this run has already applied ``inbound`` to an
    agent. The client's state describes the gate the run *started* on; once
    the resumed tools have run, a later invocation in the same run (a hook
    sending the agent back, a continuation) must not restore it again, or the
    answered gate comes back and its tools run a second time.

    ``delegates`` holds the place of every delegate this run left paused — its
    messages and interrupt state — by the id of the call that delegated to it,
    so a run in another process can resume it (``kaboo_sessions.paused.delegates``).
    """

    inbound: dict[str, Any] = field(default_factory=dict)
    agent: Any = None
    resuming: bool = False
    publishes_approvals: bool = False
    restored: bool = False
    delegates: dict[str, Any] = field(default_factory=dict)


_current_session: contextvars.ContextVar[SessionExchange | None] = contextvars.ContextVar(
    "kaboo_session_exchange", default=None
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

# The AG-UI request's forwardedProps: the host-to-runtime side channel for
# per-run context (run-scoped credentials, per-run agent config, …). Bound once
# per request like references; read by host tools/hooks via
# ``get_forwarded_props`` and by built-in features (authorized attachment
# fetching, per-invocation agent overrides).
_current_forwarded_props: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "kaboo_forwarded_props", default=None
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
    """Return the current conversation's ``thread_id``.

    Callable from any tool or hook while a run is in flight. Use it to scope
    state to one conversation, such as values a tool stores for later turns.

    Returns:
        The thread id bound for this run, or ``None`` outside a run.
    """
    return _current_thread_id.get()


def get_run_id() -> str | None:
    """Return the current request's ``run_id``.

    A new run id arrives with every request, including the one that resumes
    an interrupted turn, so it suits per-request bookkeeping such as logs and
    metrics.

    Returns:
        The run id bound for this run, or ``None`` outside a run.
    """
    return _current_run_id.get()


def get_turn_id() -> str | None:
    """Return the current logical turn's id.

    Unlike the run id, it stays the same across an interrupt and its resume, so
    it is the key to use when a tool's side effect must happen once per turn.

    Returns:
        The turn id bound for this run, or ``None`` outside a run.
    """
    return _current_turn_id.get()


@contextlib.contextmanager
def delegation_context(tool_use_id: str | None) -> Generator[None]:
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


def set_session_exchange(exchange: SessionExchange | None) -> None:
    """Bind the current request's :class:`SessionExchange`.

    Call at the start of each request handler, before creating the run task, so
    the hook that restores state and the enricher that writes it back share one
    object.
    """
    _current_session.set(exchange)


def get_session_exchange() -> SessionExchange | None:
    """Return the current request's :class:`SessionExchange` (or ``None``)."""
    return _current_session.get()


@dataclass
class StateChannel:
    """Request-scoped carrier for the shared state a run's agents write and read.

    ``state`` starts as the state the run carries and takes every state effect
    as it happens, so a later model call's workspace view is current.
    ``effects`` are the run's compiled ``state_effects``: ``(tool glob, effect)``
    pairs. ``emit`` sends each effect's operations to the client as a
    ``STATE_DELTA``; ``touched`` names the top-level keys effects changed, which
    the adapter folds into every snapshot it passes on.
    """

    state: dict[str, Any] = field(default_factory=dict)
    effects: list[tuple[str, Any]] = field(default_factory=list)
    emit: Any = None
    touched: set[str] = field(default_factory=set)


_current_state_channel: contextvars.ContextVar[StateChannel | None] = contextvars.ContextVar(
    "kaboo_state_channel", default=None
)


def set_state_channel(channel: StateChannel | None) -> None:
    """Bind the current request's :class:`StateChannel`, before creating the run task."""
    _current_state_channel.set(channel)


def get_state_channel() -> StateChannel | None:
    """Return the current request's :class:`StateChannel` (or ``None``)."""
    return _current_state_channel.get()


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


def set_forwarded_props(props: dict[str, Any] | None) -> None:
    """Bind the current request's ``RunAgentInput.forwarded_props``.

    ``forwardedProps`` is the AG-UI protocol's free-form side channel: hosts
    attach per-run context (scoped credentials, per-run agent configuration,
    tenant ids, …) that is not part of the conversation and must not reach the
    model. Call at the start of each request handler, before creating the run
    task, so tasks created afterwards inherit a copy of this context. Host
    tools and hooks read it back with :func:`get_forwarded_props`.
    """
    _current_forwarded_props.set(props if isinstance(props, dict) else {})


def get_forwarded_props() -> dict[str, Any]:
    """Return the current request's forwarded props (empty dict when none)."""
    return _current_forwarded_props.get() or {}


_current_run_context: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "kaboo_run_context", default=None
)


def set_run_context(items: list[dict[str, Any]] | None) -> None:
    """Bind the current request's context items (``RunAgentInput.context``).

    Every agent's model calls read them through
    :class:`~kaboo_workflows.hooks.ContextHook`; call before creating the run
    task so it inherits them.
    """
    _current_run_context.set(items)


def get_run_context() -> list[dict[str, Any]]:
    """Return the current request's context items (empty list when none)."""
    return _current_run_context.get() or []


@dataclass
class Principal:
    """Authenticated caller identity for the current request.

    Produced by an inbound auth verifier (see
    :func:`~kaboo_workflows.adapters.agui.create_agui_app`) and bound to the
    request context so outbound MCP auth strategies (relay) can derive a
    downstream token from the same identity.

    Attributes:
        token: The raw inbound bearer credential (no ``Bearer `` prefix), e.g.
            the user JWT or an AgentCore ``WorkloadAccessToken``. ``None`` when
            the caller was not authenticated (or auth is disabled).
        claims: Verified claims (decoded JWT payload, introspection result, …).
            Empty when the verifier only relays without validating.
        headers: Selected inbound headers the verifier chose to carry forward
            (e.g. ``WorkloadAccessToken``), so strategies can read a token from
            a non-``Authorization`` header when needed.
    """

    token: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)


_current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "kaboo_principal", default=None
)


def set_auth_context(principal: Principal | None) -> None:
    """Bind the current request's :class:`Principal`.

    Call at the start of each request handler, before creating the run task, so
    tasks (and MCP clients started within this context via
    ``contextvars.copy_context()``) inherit the identity. Outbound MCP auth
    strategies read it via :func:`get_auth_context`.
    """
    _current_principal.set(principal)


def get_auth_context() -> Principal | None:
    """Return the current request's :class:`Principal` (or ``None``)."""
    return _current_principal.get()
