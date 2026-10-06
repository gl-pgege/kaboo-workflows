"""Property: clearing tool results is idempotent and never touches a call."""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from kaboo_workflows.hooks._transcript import clear_tool_results

_TOOLS = ["athena___run_query", "query", "analyze_sql"]
_steps = st.lists(st.tuples(st.sampled_from(_TOOLS), st.integers(0, 3000)), max_size=12)


def _transcript(steps: list[tuple[str, int]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": "brief"}]}]
    for n, (name, size) in enumerate(steps):
        use = {"toolUseId": f"c{n}", "name": name, "input": {"n": n}}
        result = {"toolUseId": f"c{n}", "status": "success", "content": [{"text": "x" * size}]}
        messages.append({"role": "assistant", "content": [{"toolUse": use}]})
        messages.append({"role": "user", "content": [{"toolResult": result}]})
    return messages


def _clear(messages: list[dict[str, Any]], keep_last: int, min_chars: int) -> list[dict[str, Any]]:
    return clear_tool_results(
        messages, keep_last=keep_last, min_chars=min_chars, kept={"analyze_sql"}, tools=_TOOLS
    )


@given(_steps, st.integers(0, 5), st.integers(0, 2000))
def test_clearing_again_changes_nothing(steps, keep_last, min_chars):
    once = _clear(_transcript(steps), keep_last, min_chars)

    assert _clear(once, keep_last, min_chars) == once


@given(_steps, st.integers(0, 5), st.integers(0, 2000))
def test_clearing_keeps_every_message_and_every_call(steps, keep_last, min_chars):
    messages = _transcript(steps)

    cleared = _clear(messages, keep_last, min_chars)

    assert [m["role"] for m in cleared] == [m["role"] for m in messages]
    assert [m for m in cleared if m["role"] == "assistant"] == [
        m for m in messages if m["role"] == "assistant"
    ]
