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
import copy
import json
import logging
import threading
from typing import TYPE_CHECKING, Any, cast

from ag_ui_strands.frontend_tool_interrupt import (
    is_frontend_tool_interrupt,
    parse_frontend_tool_reason,
    wrap_frontend_tool_response,
)
from strands import Agent
from strands.agent.agent_result import AgentResult
from strands.interrupt import _InterruptState
from strands.multiagent.base import MultiAgentResult
from strands.tools.decorator import DecoratedFunctionTool, tool
from strands.types.tools import ToolContext

from .._context import delegation_context, get_session_exchange
from ._browser import BROWSER_WAIT, browser_wait_reason
from .extractors import extract_last_message, extract_text

if TYPE_CHECKING:
    from ..types import Node

logger = logging.getLogger(__name__)

_TOOL_RESULT_CONTENT_KEYS = ("document", "image", "json", "text")
_DELEGATE_ATTR = "_kaboo_delegate"
_TURN_LOCK_ATTR = "_kaboo_turn_lock"
_THREAD_LOCK_ATTR = "_kaboo_thread_lock"
_CLIENT_TOOLS_ATTR = "_kaboo_client_tools"
_SKIPPED_BROWSER_CALL = "Skipped: the person moved on without answering."


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


def _forward(interrupts: list[Any], tool_context: ToolContext) -> list[dict[str, Any]]:
    """Raise a delegate's interrupts on the coordinator and collect the answers.

    A browser tool's wait goes up as a :data:`~kaboo_workflows.tools._browser.BROWSER_WAIT`
    naming the call, so the run reports the call as pending and the browser's
    answer comes back down to it. Any other interrupt goes up as it is.
    """
    responses: list[dict[str, Any]] = []
    for interrupt in interrupts:
        if is_frontend_tool_interrupt(interrupt):
            call = parse_frontend_tool_reason(interrupt.reason)
            answer = _browser_answer(
                tool_context.interrupt(f"{BROWSER_WAIT}:{call}", reason=browser_wait_reason(call))
            )
        else:
            answer = tool_context.interrupt(interrupt.id, reason=interrupt.reason)
        responses.append({"interruptResponse": {"interruptId": interrupt.id, "response": answer}})
    return responses


def _browser_answer(answer: Any) -> Any:
    """The browser's answer inside ag-ui-strands' resume envelope, as the stand-in reads it."""
    if isinstance(answer, dict) and set(answer) == {"response"}:
        return answer["response"]
    if isinstance(answer, dict) and answer.get("cancelled") is True:
        return wrap_frontend_tool_response(_SKIPPED_BROWSER_CALL, is_error=True)
    return answer


def _save_place(node: Node, tool_context: ToolContext) -> None:
    """Keep a paused delegate's place on the session, so another process can resume it."""
    exchange = get_session_exchange()
    state = getattr(node, "_interrupt_state", None)
    if exchange is None or not isinstance(node, Agent) or state is None or not state.activated:
        return
    exchange.delegates[tool_context.tool_use["toolUseId"]] = {
        "messages": copy.deepcopy(node.messages),
        "interrupt_state": state.to_dict(),
    }


def _restore_place(node: Node, tool_context: ToolContext) -> None:
    """Put a delegate back where a run in another process left it paused."""
    exchange = get_session_exchange()
    if exchange is None or not exchange.resuming or not isinstance(node, Agent):
        return
    state = getattr(node, "_interrupt_state", None)
    if state is not None and state.activated:
        return
    place = (exchange.inbound.get("delegates") or {}).get(tool_context.tool_use["toolUseId"])
    if not isinstance(place, dict):
        return
    try:
        node._interrupt_state = _InterruptState.from_dict(place["interrupt_state"])
        node.messages = place["messages"]
    except Exception:
        logger.exception("delegate place restore failed; the delegate starts over")


def _pending_interrupt_responses(
    node: Node, tool_context: ToolContext
) -> list[dict[str, Any]] | None:
    """Propagate a delegate's already-active interrupts up to the coordinator.

    When a delegate node is resuming (its interrupt state is still activated
    from a prior turn), forward each unresolved interrupt through
    ``tool_context.interrupt`` so the coordinator captures the human response,
    then return the strands resume payload. Returns ``None`` for a fresh call.
    """
    _restore_place(node, tool_context)
    istate = getattr(node, "_interrupt_state", None)
    if not (istate and istate.activated and istate.interrupts):
        return None
    pending = [intr for intr in istate.interrupts.values() if intr.response is None]
    logger.debug("delegate resuming with %d pending interrupt(s)", len(pending))
    _save_place(node, tool_context)
    return _forward(pending, tool_context)


def _interrupt_responses_from_result(
    node: Node, result: AgentResult, tool_context: ToolContext
) -> list[dict[str, Any]]:
    """Forward interrupts raised mid-run up to the coordinator and collect responses."""
    _save_place(node, tool_context)
    return _forward(list(result.interrupts or []), tool_context)


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ", ".join(_plain(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(f"{key}: {_plain(item)}" for key, item in value.items())
    return json.dumps(value)


def _exchange(name: str, tool_input: Any, answer: str) -> str:
    try:
        reply: Any = json.loads(answer)
    except ValueError:
        reply = answer
    questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
    asked = [q["question"] for q in questions or [] if isinstance(q, dict) and q.get("question")]
    if not asked:
        return f"- {name} ({_plain(tool_input)}): {_plain(reply)}"
    if isinstance(reply, dict) and all(question in reply for question in asked):
        return "\n".join(f'- Asked "{question}": {_plain(reply[question])}' for question in asked)
    return "\n".join(f'- Asked "{question}"' for question in asked) + f"\n  Answer: {_plain(reply)}"


def _browser_exchanges(node: Node) -> list[str]:
    """What the delegate asked the person through browser tools in its last task, and the answers."""
    names = getattr(node, _CLIENT_TOOLS_ATTR, None)
    messages = getattr(node, "messages", None)
    if not names or not isinstance(messages, list):
        return []
    start = 0
    for index, message in enumerate(messages):
        content = message.get("content") or []
        if message.get("role") == "user" and any("text" in block for block in content):
            start = index
    calls: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    for message in messages[start:]:
        for block in message.get("content") or []:
            use = block.get("toolUse")
            if use and use.get("name") in names:
                calls[use["toolUseId"]] = use
            result = block.get("toolResult")
            if result and result.get("toolUseId") in calls:
                use = calls[result["toolUseId"]]
                answer = " ".join(part.get("text", "") for part in result.get("content") or [])
                lines.append(_exchange(use["name"], use.get("input"), answer.strip()))
    return lines


def _with_exchanges(node: Node, tool_result: dict[str, Any]) -> dict[str, Any]:
    """Tell the coordinator what the delegate asked the person, so it knows the answers too."""
    lines = _browser_exchanges(node)
    if not lines:
        return tool_result
    note = "While working, this agent asked the person:\n" + "\n".join(lines)
    content = list(tool_result["content"])
    if content and "text" in content[-1]:
        content[-1] = {"text": f"{content[-1]['text']}\n\n{note}"}
    else:
        content.append({"text": note})
    return {**tool_result, "content": content}


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
                        responses = _interrupt_responses_from_result(node, result, tool_context)
                        result = await node.invoke_async(cast(Any, responses))
                    return _with_exchanges(node, _result_to_tool_result(result))

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
                    responses = _interrupt_responses_from_result(node, result, tool_context)
                    result = node(cast(Any, responses))
                return _with_exchanges(node, _result_to_tool_result(result))

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
