"""Single choke point for AWS Strands / ag-ui-strands internal access.

Every function here reaches into a non-public attribute of a third-party
package (``StrandsAgent._agents_by_thread``, ``Agent._interrupt_state``,
``_build_strands_history``). Concentrating them in one module means an upstream
change breaks in exactly one place instead of being scattered across the
adapter, and each seam is documented as "depends on internal".
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from ag_ui_strands import StrandsAgent

DEFAULT_THREAD = "default"

# End-of-stream sentinel for the SSE merge queue: ``agui._consume_run`` puts it
# on the queue so ``agui._sse_response`` recognises completion.
STREAM_DONE = object()


def get_thread_agent(agui_agent: StrandsAgent, thread_id: str | None) -> Any | None:
    """Return the per-thread ``strands.Agent`` for a conversation.

    DEPENDS ON ag-ui-strands internal: ``StrandsAgent._agents_by_thread``.
    """
    return agui_agent._agents_by_thread.get(thread_id or DEFAULT_THREAD)


async def ensure_thread_agent(agui_agent: StrandsAgent, input_data: Any) -> Any:
    """Return the thread's agent, creating it when this process has never run it.

    ag-ui-strands creates per-thread agents lazily *inside* ``run()``. That is too
    late for resume, which has to install the interrupt responses on the agent
    before the run starts. On a warm process the agent is already there; on a cold
    one it is not, and a resume would fail even though the client still holds the
    pending gate — losing an approval to nothing more than a restart.

    Mirrors the construction in ``StrandsAgent.run()`` so a seeded agent is
    indistinguishable from one ag-ui-strands would have built itself, including
    the per-thread session manager.

    DEPENDS ON ag-ui-strands internals: ``_agents_by_thread``, ``_model``,
    ``_system_prompt``, ``_tools``, ``_agent_kwargs``, ``_hooks`` and
    ``config.session_manager_provider``.
    """
    thread_id = getattr(input_data, "thread_id", None) or DEFAULT_THREAD
    existing = agui_agent._agents_by_thread.get(thread_id)
    if existing is not None:
        return existing

    from strands import Agent as StrandsAgentCore

    session_manager = None
    provider = getattr(agui_agent.config, "session_manager_provider", None)
    if provider is not None:
        session_manager = provider(input_data)
        if inspect.isawaitable(session_manager):
            session_manager = await session_manager

    kwargs = dict(agui_agent._agent_kwargs)
    # Omitted rather than passed empty, as ag-ui-strands does: a future strands
    # release could read `hooks=[]` as "no default hooks".
    if agui_agent._hooks:
        kwargs["hooks"] = list(agui_agent._hooks)
    agent = StrandsAgentCore(
        model=agui_agent._model,
        system_prompt=agui_agent._system_prompt,
        tools=cast("list[Any]", agui_agent._tools),
        session_manager=session_manager,
        **kwargs,
    )
    agui_agent._agents_by_thread[thread_id] = agent
    return agent


def seed_history(strands_agent: Any, input_data: Any) -> None:
    """Give an agent seeded for a resume the conversation the client holds.

    ag-ui-strands rebuilds an agent's history from the client's transcript on a
    fresh run, but a resume continues from the agent's own checkpoint, so an
    agent this process never ran must get that history first.

    DEPENDS ON ag-ui-strands internal: ``_build_strands_history``.
    """
    from ag_ui_strands.agent import _build_strands_history

    strands_agent.messages = _build_strands_history(
        list(getattr(input_data, "messages", None) or [])
    )


def get_interrupt_state(strands_agent: Any) -> Any | None:
    """Return the agent's interrupt state.

    DEPENDS ON strands internal: ``Agent._interrupt_state``.
    """
    return getattr(strands_agent, "_interrupt_state", None)


def is_interrupt_active(strands_agent: Any) -> bool:
    """Whether the agent is currently paused on an interrupt."""
    istate = get_interrupt_state(strands_agent)
    return bool(istate is not None and getattr(istate, "activated", False))


def pending_interrupts(strands_agent: Any, *, unresolved_only: bool = True) -> dict[str, Any]:
    """Return the agent's pending interrupts keyed by interrupt id.

    DEPENDS ON strands internal: ``_InterruptState.interrupts`` and
    ``Interrupt.response``.
    """
    return pending_interrupts_of(
        get_interrupt_state(strands_agent), unresolved_only=unresolved_only
    )


def pending_interrupts_of(istate: Any, *, unresolved_only: bool = True) -> dict[str, Any]:
    """Return the pending interrupts of an ``_InterruptState``.

    DEPENDS ON strands internal: ``_InterruptState.interrupts`` and
    ``Interrupt.response``.
    """
    if istate is None or not getattr(istate, "interrupts", None):
        return {}
    if unresolved_only:
        return {iid: intr for iid, intr in istate.interrupts.items() if intr.response is None}
    return dict(istate.interrupts)
