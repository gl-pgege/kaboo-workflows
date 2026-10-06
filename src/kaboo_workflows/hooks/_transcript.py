"""Pure transforms over strands conversation messages.

Every function takes a list of strands ``Message`` dicts and returns a list,
never mutating its input. That is what lets a hook reshape the copy of a
conversation a model is sent, or the copy written back to the client, while the
conversation the agent keeps — and the chat the client renders — stay as they
were.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from typing import Any

MCP_TOOL_SEPARATOR = "___"
DEFAULT_MIN_CHARS = 1500
SUMMARY_BRIEF = "Summary of your earlier work in this conversation"
SEEDED_MESSAGES = "kaboo_seeded_messages"

_DELEGATE_INPUT_CHARS = 2000
_MCP_INPUT_CHARS = 500
_RESULT_EXCERPT_CHARS = 500
_CLEARED_PREFIX = "Result cleared ("
_CLEARED_SUFFIX = "): call it again to see it."
_ID_SUFFIXES = ("id", "Id", "ID")
_MAX_IDS = 10
_REMEMBER = re.compile(
    r"^[ \t]*(?:(#{1,6})[ \t]+)?(?:\*\*|__)?to remember\b", re.IGNORECASE | re.MULTILINE
)
_HEADING = re.compile(r"^[ \t]*(#{1,6})[ \t]+\S", re.MULTILINE)


def is_mcp_tool(name: str) -> bool:
    """Whether ``name`` is an MCP tool (``<target>___<tool>``) rather than a delegate."""
    return MCP_TOOL_SEPARATOR in name


def estimate_tokens(messages: list[dict[str, Any]], system_prompt: Any = None) -> int:
    """Estimate the tokens of ``messages`` and ``system_prompt`` at four characters each."""
    text = json.dumps(messages, ensure_ascii=False, default=str)
    if system_prompt:
        text += (
            system_prompt
            if isinstance(system_prompt, str)
            else json.dumps(system_prompt, ensure_ascii=False, default=str)
        )
    return len(text) // 4


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


def clear_tool_results(
    messages: list[dict[str, Any]],
    *,
    keep_last: int,
    min_chars: int,
    kept: Collection[str],
    tools: Collection[str],
) -> list[dict[str, Any]]:
    """Replace every ``toolResult`` but the last ``keep_last`` with a placeholder.

    The placeholder reads ``Result cleared (<tool>[, <key>=<value>…]): call it
    again to see it.``, carrying up to ten pairs from the result's JSON whose key
    ends in ``id``/``Id``/``ID``. A result is left alone when it is shorter than
    ``min_chars`` characters, already a placeholder, or never cleared — its tool
    is in ``kept``, or is a delegate missing from ``tools``. The ``toolUse``
    blocks are kept, so the model can make the call again. The output depends on
    nothing but the input, and clearing it again changes nothing, so a result
    reads the same on every later call once it has been cleared.

    Args:
        messages: The conversation, oldest first.
        keep_last: How many of the most recent results are always kept.
        min_chars: Results shorter than this are always kept.
        kept: Tools whose results are never cleared (``keep_tools`` plus the
            agent's delegates).
        tools: Names of the tools the agent has.

    Returns:
        The conversation with the older results cleared.
    """
    names = _tool_names(messages)
    places = [
        (i, j)
        for i, message in enumerate(messages)
        for j, block in enumerate(message.get("content") or [])
        if "toolResult" in block
    ]
    cleared = list(messages)
    for i, j in places[: max(len(places) - keep_last, 0)]:
        result = cleared[i]["content"][j]["toolResult"]
        name = names.get(result.get("toolUseId"), "")
        if (
            _never_cleared(name, kept, tools)
            or _is_cleared(result)
            or _result_chars(result) < min_chars
        ):
            continue
        content = list(cleared[i]["content"])
        content[j] = {"toolResult": {**result, "content": [{"text": cleared_text(name, result)}]}}
        cleared[i] = {**cleared[i], "content": content}
    return cleared


def cleared_text(name: str, result: dict[str, Any]) -> str:
    """The placeholder that replaces a cleared result of tool ``name``."""
    label = ", ".join([name or "tool", *_result_ids(result)])
    return f"{_CLEARED_PREFIX}{label}{_CLEARED_SUFFIX}"


def _is_cleared(result: dict[str, Any]) -> bool:
    content = result.get("content") or []
    text = content[0].get("text") if len(content) == 1 else None
    return (
        isinstance(text, str)
        and text.startswith(_CLEARED_PREFIX)
        and text.endswith(_CLEARED_SUFFIX)
    )


def _never_cleared(name: str, kept: Collection[str], tools: Collection[str]) -> bool:
    return name in kept or (name not in tools and not is_mcp_tool(name))


def _tool_names(messages: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for block in message.get("content") or []:
            use = block.get("toolUse")
            if use is not None:
                names[use.get("toolUseId")] = str(use.get("name") or "")
    return names


def _result_chars(result: dict[str, Any]) -> int:
    total = 0
    for block in result.get("content") or []:
        if "text" in block:
            total += len(str(block["text"]))
        elif "json" in block:
            total += len(json.dumps(block["json"], ensure_ascii=False, default=str))
        else:
            total += len(str(block))
    return total


def _result_ids(result: dict[str, Any]) -> list[str]:
    pairs: list[str] = []
    for block in result.get("content") or []:
        if "json" in block:
            _collect_ids(block["json"], pairs)
        elif isinstance(block.get("text"), str):
            try:
                _collect_ids(json.loads(block["text"]), pairs)
            except ValueError:
                continue
    return pairs[:_MAX_IDS]


def _collect_ids(value: Any, pairs: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_ids(item, pairs)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if len(pairs) >= _MAX_IDS:
            return
        if _is_id(key, item):
            pair = f"{key}={item}"
            if pair not in pairs:
                pairs.append(pair)
        else:
            _collect_ids(item, pairs)


def _is_id(key: Any, value: Any) -> bool:
    return (
        isinstance(key, str)
        and key.endswith(_ID_SUFFIXES)
        and isinstance(value, str | int | float)
        and not isinstance(value, bool)
    )


def mask_transcript(
    messages: list[dict[str, Any]],
    *,
    min_chars: int,
    kept: Collection[str],
    tools: Collection[str],
) -> list[dict[str, Any]]:
    """Mask every finished call of a sub-agent's transcript.

    A finished call keeps its brief, its text and ``toolUse`` blocks — its final
    report among them — and loses its ``reasoningContent``; each of its tool
    results is cleared as :func:`clear_tool_results` would with ``keep_last=0``.
    A call with an unanswered ``toolUse``, such as one paused on an interrupt,
    is kept whole. Masking a masked transcript changes nothing.

    Args:
        messages: The transcript, oldest first.
        min_chars: Results shorter than this are kept.
        kept: Tools whose results are never cleared.
        tools: Names of the tools the agent has.

    Returns:
        The masked transcript.
    """
    masked: list[dict[str, Any]] = []
    for call in split_calls(messages):
        if _is_open(call):
            masked.extend(call)
            continue
        cleared = clear_tool_results(call, keep_last=0, min_chars=min_chars, kept=kept, tools=tools)
        masked.extend(
            {**m, "content": [b for b in m.get("content") or [] if "reasoningContent" not in b]}
            for m in cleared
        )
    return _join_turns(masked)


def split_calls(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split a transcript into calls, each opened by a user message with text and no result."""
    calls: list[list[dict[str, Any]]] = []
    for message in messages:
        if not calls or _opens_call(message):
            calls.append([message])
        else:
            calls[-1].append(message)
    return calls


def _opens_call(message: dict[str, Any]) -> bool:
    content = message.get("content") or []
    return (
        message.get("role") == "user"
        and any("text" in block for block in content)
        and not any("toolResult" in block for block in content)
    )


def _is_open(call: list[dict[str, Any]]) -> bool:
    blocks = [block for message in call for block in message.get("content") or []]
    asked = {b["toolUse"].get("toolUseId") for b in blocks if "toolUse" in b}
    answered = {b["toolResult"].get("toolUseId") for b in blocks if "toolResult" in b}
    return not asked <= answered


def split_for_summary(
    messages: list[dict[str, Any]], budget: int
) -> tuple[list[list[dict[str, Any]]], list[list[dict[str, Any]]]]:
    """Split a transcript's calls into the oldest, to summarise, and the newest, to keep.

    The kept calls are the newest whose estimated tokens fit ``budget``, and
    always include the newest call.
    """
    calls = split_calls(messages)
    kept = calls[-1:]
    size = sum(estimate_tokens(call) for call in kept)
    for call in reversed(calls[:-1]):
        size += estimate_tokens(call)
        if size > budget:
            break
        kept.insert(0, call)
    return calls[: len(calls) - len(kept)], kept


def remembered_blocks(calls: list[list[dict[str, Any]]]) -> list[str]:
    """Collect the ``To remember`` blocks of the reports in ``calls``, once each, in order.

    A summary pair contributes the blocks it already carries.
    """
    blocks: list[str] = []
    for call in calls:
        if _is_summary(call):
            carried = call[1].get("content", [])[1:] if len(call) > 1 else []
            blocks.extend(str(block["text"]) for block in carried if "text" in block)
        else:
            blocks.extend(to_remember_blocks(_report(call)))
    return list(dict.fromkeys(blocks))


def to_remember_blocks(text: str) -> list[str]:
    """Return the blocks of ``text`` that open with a ``To remember`` heading or line.

    A block opened by a heading runs to the next heading of the same or a higher
    level; one opened by a plain line runs to the next heading of any level;
    either may run to the end of the text. Blocks are returned verbatim.
    """
    blocks: list[str] = []
    end = 0
    for match in _REMEMBER.finditer(text):
        if match.start() < end:
            continue
        level = len(match.group(1)) if match.group(1) else 7
        end = len(text)
        line_end = text.find("\n", match.end())
        if line_end != -1:
            for heading in _HEADING.finditer(text, line_end + 1):
                if len(heading.group(1)) <= level:
                    end = heading.start()
                    break
        blocks.append(text[match.start() : end].rstrip())
    return blocks


def render_calls(calls: list[list[dict[str, Any]]]) -> str:
    """Render calls as plain text for a model to summarise."""
    sections: list[str] = []
    for call in calls:
        lines: list[str] = []
        for message in call:
            speaker = "Brief" if message.get("role") == "user" else "Agent"
            for block in message.get("content") or []:
                if "text" in block:
                    lines.append(f"{speaker}: {block['text']}")
                elif "toolUse" in block:
                    use = block["toolUse"]
                    tool_input = json.dumps(use.get("input", {}), ensure_ascii=False, default=str)
                    lines.append(f"Called {use.get('name')} with {tool_input}")
                elif "toolResult" in block:
                    lines.append(f"Result: {result_text(block['toolResult'])}")
        sections.append("\n".join(lines))
    return "\n\n---\n\n".join(sections)


def summary_pair(summary: str, remembered: list[str]) -> list[dict[str, Any]]:
    """The leading user/assistant pair that stands in for summarised calls."""
    return [
        {"role": "user", "content": [{"text": SUMMARY_BRIEF}]},
        {
            "role": "assistant",
            "content": [{"text": summary}, *({"text": block} for block in remembered)],
        },
    ]


def _is_summary(call: list[dict[str, Any]]) -> bool:
    if not call:
        return False
    content = call[0].get("content") or []
    return len(content) == 1 and content[0].get("text") == SUMMARY_BRIEF


def _report(call: list[dict[str, Any]]) -> str:
    for message in reversed(call):
        if message.get("role") != "assistant":
            continue
        texts = [str(b["text"]) for b in message.get("content") or [] if "text" in b]
        if texts:
            return "\n".join(texts)
    return ""


def strip_cache_points(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop every ``cachePoint`` block, and any message that leaves empty."""
    if not any("cachePoint" in b for m in messages for b in m.get("content") or []):
        return messages
    stripped = [
        {**m, "content": [b for b in m.get("content") or [] if "cachePoint" not in b]}
        for m in messages
    ]
    return [m for m in stripped if m["content"]]


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
