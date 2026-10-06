"""Pure transforms over strands conversation messages.

Every function takes a list of strands ``Message`` dicts and returns a list,
never mutating its input. That is what lets a hook reshape the copy of a
conversation a model is sent, or the copy written back to the client, while the
conversation the agent keeps — and the chat the client renders — stay as they
were.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

MCP_TOOL_SEPARATOR = "___"

_DELEGATE_INPUT_CHARS = 2000
_MCP_INPUT_CHARS = 500
_RESULT_EXCERPT_CHARS = 500


def is_mcp_tool(name: str) -> bool:
    """Whether ``name`` is an MCP tool (``<target>___<tool>``) rather than a delegate."""
    return MCP_TOOL_SEPARATOR in name


def result_text(result: dict[str, Any]) -> str:
    """Render a ``toolResult``'s content as text, ``json`` blocks as JSON."""
    parts: list[str] = []
    for block in result.get("content") or []:
        if "text" in block:
            parts.append(str(block["text"]))
        elif "json" in block:
            parts.append(json.dumps(block["json"], ensure_ascii=False, default=str))
        else:
            parts.append(f"[{next(iter(block), 'content')}]")
    return "\n".join(parts)


def foreign_calls_to_notes(
    messages: list[dict[str, Any]], tools: Collection[str]
) -> list[dict[str, Any]]:
    """Replace calls to tools outside ``tools`` with text notes.

    Each such ``toolUse`` becomes a text block in its assistant message —
    ``Earlier step (<name>, a tool you don't have): asked <input>; returned
    <result>`` — and its ``toolResult`` is dropped. A delegate (a name without
    ``___``) keeps up to 2,000 characters of input and its whole result; an MCP
    tool keeps 500 characters of input and a 500-character excerpt of its
    result. Notes go before the message's remaining ``toolUse`` blocks, a user
    message left empty is dropped, and the assistant messages it separated are
    joined, so the result still alternates and pairs every call with its result.

    Args:
        messages: The conversation, oldest first.
        tools: Names of the tools the agent about to run has.

    Returns:
        The rewritten conversation, or ``messages`` itself when no call is foreign.
    """
    foreign: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            use = block.get("toolUse")
            if use is not None and use.get("name") not in tools:
                foreign[use.get("toolUseId")] = use
    if not foreign:
        return messages

    results: dict[str, dict[str, Any]] = {}
    for message in messages:
        for block in message.get("content") or []:
            result = block.get("toolResult")
            if result is not None and result.get("toolUseId") in foreign:
                results[result["toolUseId"]] = result

    rewritten: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content") or []
        if message.get("role") == "assistant" and any(_calls(block, foreign) for block in content):
            leading = [
                {"text": _note(block["toolUse"], results.get(block["toolUse"].get("toolUseId")))}
                if _calls(block, foreign)
                else block
                for block in content
                if "toolUse" not in block or _calls(block, foreign)
            ]
            own_calls = [b for b in content if "toolUse" in b and not _calls(b, foreign)]
            rewritten.append({**message, "content": [*leading, *own_calls]})
        elif any(_answers(block, foreign) for block in content):
            rewritten.append(
                {**message, "content": [b for b in content if not _answers(b, foreign)]}
            )
        else:
            rewritten.append(message)
    return _join_turns(rewritten)


def _calls(block: dict[str, Any], uses: dict[str, Any]) -> bool:
    return "toolUse" in block and block["toolUse"].get("toolUseId") in uses


def _answers(block: dict[str, Any], uses: dict[str, Any]) -> bool:
    return "toolResult" in block and block["toolResult"].get("toolUseId") in uses


def _note(use: dict[str, Any], result: dict[str, Any] | None) -> str:
    name = str(use.get("name") or "tool")
    mcp = is_mcp_tool(name)
    asked = _cut(
        json.dumps(use.get("input", {}), ensure_ascii=False, default=str),
        _MCP_INPUT_CHARS if mcp else _DELEGATE_INPUT_CHARS,
    )
    if result is None:
        returned = "no result"
    else:
        returned = result_text(result)
        if mcp and len(returned) > _RESULT_EXCERPT_CHARS:
            returned = f"{returned[:_RESULT_EXCERPT_CHARS]}… [{len(returned):,} characters in full]"
    return f"Earlier step ({name}, a tool you don't have): asked {asked}; returned {returned}"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _join_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty messages and join neighbours that share a role."""
    joined: list[dict[str, Any]] = []
    for message in messages:
        if not message.get("content"):
            continue
        if joined and joined[-1].get("role") == message.get("role"):
            joined[-1] = {**joined[-1], "content": [*joined[-1]["content"], *message["content"]]}
        else:
            joined.append(message)
    return joined
