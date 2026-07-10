"""Single choke point for AWS Strands / ag-ui-strands internal access.

Every function here reaches into a non-public attribute of a third-party
package (``StrandsAgent._agents_by_thread``, ``Agent._interrupt_state``,
``Agent.messages``, ``Agent.stream_async``). Concentrating them in one module
means an upstream change breaks in exactly one place instead of being scattered
across the adapter, and each seam is documented as "depends on internal".

If ag-ui-strands ever grows a first-class resume entry point,
:func:`resume_prompt_override` is the only thing that needs to change.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ag_ui_strands import StrandsAgent

DEFAULT_THREAD = "default"

# Shared end-of-stream sentinel for the SSE merge queue. Both the Agent path
# (``agui._consume_run``) and the multi-agent path (``_multiagent.consume``)
# put this on the queue so ``agui._sse_response`` recognises completion
# regardless of which runner produced the stream.
STREAM_DONE = object()


def get_thread_agent(agui_agent: StrandsAgent, thread_id: str | None) -> Any | None:
    """Return the per-thread ``strands.Agent`` for a conversation.

    DEPENDS ON ag-ui-strands internal: ``StrandsAgent._agents_by_thread``.
    """
    return agui_agent._agents_by_thread.get(thread_id or DEFAULT_THREAD)


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
    istate = get_interrupt_state(strands_agent)
    if istate is None or not getattr(istate, "interrupts", None):
        return {}
    if unresolved_only:
        return {
            iid: intr for iid, intr in istate.interrupts.items() if intr.response is None
        }
    return dict(istate.interrupts)


def deactivate_interrupts(strands_agent: Any) -> None:
    """Clear a stale, activated interrupt state.

    DEPENDS ON strands internal: ``_InterruptState.deactivate``.
    """
    istate = get_interrupt_state(strands_agent)
    if istate is not None and getattr(istate, "activated", False):
        istate.deactivate()


@contextlib.contextmanager
def resume_prompt_override(strands_agent: Any, responses: list[dict[str, Any]]) -> Iterator[None]:
    """Route the next ag-ui-strands run through interrupt *responses*.

    ``ag_ui_strands.StrandsAgent.run()`` has no resume entry point — it always
    calls ``strands_agent.stream_async(prompt)`` with a user message (or
    ``None``). To resume an interrupted agent we must instead feed the interrupt
    ``responses`` into ``stream_async`` while still using ``run()``'s full
    strands->AG-UI event conversion pipeline.

    We therefore override ``stream_async`` on the instance for the duration of
    the run and guarantee restoration in ``finally``. The agent's message
    history is captured up-front and re-applied inside the override so the
    replay path in ``run()`` cannot overwrite it before the resume.

    DEPENDS ON strands internal: ``Agent.stream_async`` / ``Agent.messages``.
    """
    original_stream = strands_agent.stream_async
    saved_messages = list(strands_agent.messages) if strands_agent.messages else []

    async def _resume_stream(prompt: Any = None, **kwargs: Any) -> Any:
        strands_agent.messages = saved_messages
        async for evt in original_stream(responses, **kwargs):
            yield evt

    strands_agent.stream_async = _resume_stream
    try:
        yield
    finally:
        strands_agent.stream_async = original_stream
