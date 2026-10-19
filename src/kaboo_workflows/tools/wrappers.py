"""Node wrapping utilities for delegation.

Provides ``node_as_tool`` and ``node_as_async_tool`` for wrapping
``Agent`` / ``MultiAgentBase`` nodes as ``AgentTool`` instances.

Key Features:
    - Sync and async tool wrappers for Agent and MultiAgentBase nodes
    - Automatic tool name resolution from agent_id or node id
    - Message content preservation from single-agent and multi-agent results
    - Native strands interrupt propagation via tool_context.interrupt()
    - Calls to one node made in the same step take turns instead of failing
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any, cast

from strands import Agent
from strands.agent.agent_result import AgentResult
from strands.multiagent.base import MultiAgentResult
from strands.tools.decorator import DecoratedFunctionTool, tool
from strands.types.tools import ToolContext

from .._context import delegation_context
from .extractors import extract_last_message, extract_text

if TYPE_CHECKING:
    from ..types import Node

logger = logging.getLogger(__name__)

_TOOL_RESULT_CONTENT_KEYS = ("document", "image", "json", "text")
_DELEGATE_ATTR = "_kaboo_delegate"
_TURN_LOCK_ATTR = "_kaboo_turn_lock"
_THREAD_LOCK_ATTR = "_kaboo_thread_lock"


def _turn_lock(node: Node) -> asyncio.Lock:
    """The lock that has calls to ``node`` on the running loop take turns.

    A node handles one call at a time (strands refuses a second with
    ``ConcurrencyException``), so when a coordinator asks one delegate for two
    things in the same step, the second call waits for the first.
    """
    loop = asyncio.get_running_loop()
    held = getattr(node, _TURN_LOCK_ATTR, None)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        setattr(node, _TURN_LOCK_ATTR, held)
    return held[1]


def _thread_lock(node: Node) -> threading.Lock:
    """The lock that has sync calls to ``node`` take turns, made when the node is wrapped."""
    lock = getattr(node, _THREAD_LOCK_ATTR, None)
    if lock is None:
        lock = threading.Lock()
        setattr(node, _THREAD_LOCK_ATTR, lock)
    return lock


def delegate_tool_names(agent: Agent) -> set[str]:
    """Return the names of ``agent``'s tools that run another node.

    Those are the tools :func:`node_as_tool` and :func:`node_as_async_tool` make.
    """
    return {
        name
        for name, tool in agent.tool_registry.registry.items()
        if getattr(tool, _DELEGATE_ATTR, False)
    }


def _resolve_tool_name(node: Node, name: str | None) -> str:
    if name is not None:
        return name
    if isinstance(node, Agent):
        return node.agent_id
    return getattr(node, "id", "sub_orchestration")


def _result_to_tool_result(result: AgentResult | MultiAgentResult) -> dict[str, Any]:
    message = extract_last_message(result)
    content: list[dict[str, Any]] = []
    for block in message.get("content", []):
        source_block = cast(dict[str, Any], block)
        tool_result_block = {
            key: source_block[key] for key in _TOOL_RESULT_CONTENT_KEYS if key in source_block
        }
        if tool_result_block:
            content.append(tool_result_block)

    if content:
        return {"status": "success", "content": content}

    structured = result.structured_output if isinstance(result, AgentResult) else None
    if structured is not None:
        return {"status": "success", "content": [{"text": structured.model_dump_json()}]}

    return {"status": "success", "content": [{"text": extract_text(message)}]}


def _pending_interrupt_responses(
    node: Node, tool_context: ToolContext
) -> list[dict[str, Any]] | None:
    """Propagate a delegate's already-active interrupts up to the coordinator.

    When a delegate node is resuming (its interrupt state is still activated
    from a prior turn), forward each unresolved interrupt through
    ``tool_context.interrupt`` so the coordinator captures the human response,
    then return the strands resume payload. Returns ``None`` for a fresh call.
    """
    istate = getattr(node, "_interrupt_state", None)
    if not (istate and istate.activated and istate.interrupts):
        return None
    pending = {iid: intr for iid, intr in istate.interrupts.items() if intr.response is None}
    logger.debug("delegate resuming with %d pending interrupt(s)", len(pending))
    responses: list[dict[str, Any]] = []
    for intr_id, intr in pending.items():
        user_response = tool_context.interrupt(intr_id, reason=intr.reason)
        responses.append({"interruptResponse": {"interruptId": intr_id, "response": user_response}})
    return responses


def _interrupt_responses_from_result(
    result: AgentResult, tool_context: ToolContext
) -> list[dict[str, Any]]:
    """Forward interrupts raised mid-run up to the coordinator and collect responses."""
    responses: list[dict[str, Any]] = []
    for intr in result.interrupts or []:
        user_response = tool_context.interrupt(intr.id, reason=intr.reason)
        responses.append({"interruptResponse": {"interruptId": intr.id, "response": user_response}})
    return responses


def _node_as_tool(
    node: Node,
    *,
    name: str | None,
    description: str,
    is_async: bool,
) -> DecoratedFunctionTool:
    """Shared implementation for :func:`node_as_tool` / :func:`node_as_async_tool`.

    Uses strands-native ``tool_context.interrupt()`` for human-in-the-loop; the
    coordinator handles all interrupt state management automatically. Calls made
    in the same step take turns, since a node handles one call at a time.
    """
    tool_name = _resolve_tool_name(node, name)

    if is_async:

        @tool(name=tool_name, description=description, context=True)
        async def delegate(tool_context: ToolContext, input: str) -> dict[str, Any]:
            async with _turn_lock(node):
                with delegation_context(tool_context.tool_use.get("toolUseId")):
                    resume = _pending_interrupt_responses(node, tool_context)
                    result = await node.invoke_async(
                        cast(Any, resume if resume is not None else input)
                    )
                    while (
                        isinstance(result, AgentResult)
                        and result.stop_reason == "interrupt"
                        and result.interrupts
                    ):
                        responses = _interrupt_responses_from_result(result, tool_context)
                        result = await node.invoke_async(cast(Any, responses))
                    return _result_to_tool_result(result)

    else:
        lock = _thread_lock(node)

        @tool(name=tool_name, description=description, context=True)
        def delegate(tool_context: ToolContext, input: str) -> dict[str, Any]:
            with lock, delegation_context(tool_context.tool_use.get("toolUseId")):
                resume = _pending_interrupt_responses(node, tool_context)
                result = node(cast(Any, resume if resume is not None else input))
                while (
                    isinstance(result, AgentResult)
                    and result.stop_reason == "interrupt"
                    and result.interrupts
                ):
                    responses = _interrupt_responses_from_result(result, tool_context)
                    result = node(cast(Any, responses))
                return _result_to_tool_result(result)

    setattr(delegate, _DELEGATE_ATTR, True)
    return delegate


def node_as_tool(
    node: Node,
    *,
    name: str | None = None,
    description: str,
) -> DecoratedFunctionTool:
    """Wrap an Agent or MultiAgentBase as a sync ``AgentTool`` for delegation."""
    return _node_as_tool(node, name=name, description=description, is_async=False)


def node_as_async_tool(
    node: Node,
    *,
    name: str | None = None,
    description: str,
) -> DecoratedFunctionTool:
    """Wrap an Agent or MultiAgentBase as an async ``AgentTool`` for delegation."""
    return _node_as_tool(node, name=name, description=description, is_async=True)
