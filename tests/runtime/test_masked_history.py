"""A ``keep: masked`` history writes back a masked copy of each finished call.

The run keeps the whole transcript, so a second call in the same run sees
everything; a memory stored in full is masked when the agent loads it; a call
paused on an interrupt is written back whole.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from strands import Agent
from strands.hooks import HookRegistry
from strands.hooks.events import AfterInvocationEvent, BeforeInvocationEvent

from kaboo_workflows._context import HistoryExchange, set_history_exchange
from kaboo_workflows.hooks import HistoryHook, HistoryPolicy
from kaboo_workflows.hooks._transcript import mask_transcript
from tests.fakes import FakeModel

BIG = json.dumps({"queryExecutionId": "q-1", "padding": "x" * 2000})
PLACEHOLDER = "Result cleared (athena___run_query, queryExecutionId=q-1): call it again to see it."
MASKED = HistoryPolicy(keep="masked")


def _result(tool_use_id: str, text: str) -> dict[str, Any]:
    return {
        "toolResult": {"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]}
    }


def _call(n: int, *, result: str = BIG, name: str = "athena___run_query") -> list[Any]:
    """A finished call: its brief, reasoning and a tool call, the result, a report."""
    return [
        {"role": "user", "content": [{"text": f"brief {n}"}]},
        {
            "role": "assistant",
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "thinking", "signature": "s"}}},
                {"text": f"checking {n}"},
                {"toolUse": {"toolUseId": f"c{n}", "name": name, "input": {"n": n}}},
            ],
        },
        {"role": "user", "content": [_result(f"c{n}", result)]},
        {"role": "assistant", "content": [{"text": f"report {n}"}]},
    ]


def _masked_call(n: int) -> list[Any]:
    return [
        {"role": "user", "content": [{"text": f"brief {n}"}]},
        {
            "role": "assistant",
            "content": [
                {"text": f"checking {n}"},
                {
                    "toolUse": {
                        "toolUseId": f"c{n}",
                        "name": "athena___run_query",
                        "input": {"n": n},
                    }
                },
            ],
        },
        {"role": "user", "content": [_result(f"c{n}", PLACEHOLDER)]},
        {"role": "assistant", "content": [{"text": f"report {n}"}]},
    ]


def _paused_call(n: int) -> list[Any]:
    """A call stopped on an ask_user interrupt: its last tool call has no result."""
    question = {"toolUse": {"toolUseId": f"q{n}", "name": "ask_user", "input": {"question": "?"}}}
    return [*_call(n)[:3], {"role": "assistant", "content": [question]}]


def _mask(messages: list[Any], **overrides: Any) -> list[Any]:
    options: dict[str, Any] = {
        "min_chars": 1500,
        "kept": set(),
        "tools": {"athena___run_query", "ask_user"},
        **overrides,
    }
    return mask_transcript(messages, **options)


def _result_texts(messages: list[Any]) -> list[str]:
    return [
        b["toolResult"]["content"][0]["text"]
        for m in messages
        for b in m["content"]
        if "toolResult" in b
    ]


def test_a_finished_call_keeps_brief_text_calls_and_report_but_not_results_or_reasoning() -> None:
    assert _mask(_call(1)) == _masked_call(1)


@pytest.mark.parametrize(
    ("call", "overrides"),
    [
        pytest.param(_call(1, result="3 rows"), {}, id="shorter-than-min-chars"),
        pytest.param(_call(1), {"kept": {"athena___run_query"}}, id="keep-tools"),
        pytest.param(_call(1, name="researcher"), {}, id="delegate-report"),
    ],
)
def test_some_results_survive_masking(call: list[Any], overrides: dict[str, Any]) -> None:
    assert _result_texts(_mask(call, **overrides)) == _result_texts(call)


def test_masking_drops_the_structured_copy_of_every_result() -> None:
    call = _call(1, result="3 rows")
    call[2]["content"][0]["toolResult"]["structuredContent"] = {"rows": 3}

    masked = _mask(call)

    assert _result_texts(masked) == ["3 rows"]
    assert all(
        "structuredContent" not in b.get("toolResult", {}) for m in masked for b in m["content"]
    )


def test_a_call_paused_on_an_interrupt_is_kept_whole() -> None:
    assert _mask([*_call(1), *_paused_call(2)]) == [*_masked_call(1), *_paused_call(2)]


def test_masking_a_masked_transcript_changes_nothing() -> None:
    once = _mask([*_call(1), *_call(2)])

    assert _mask(once) == once


# ── HistoryHook with a masked policy ─────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_exchange() -> Iterator[None]:
    set_history_exchange(None)
    yield
    set_history_exchange(None)


def _fire(hook: HistoryHook, agent: Agent, event_type: Any) -> None:
    registry = HookRegistry()
    registry.add_hook(hook)
    registry.invoke_callbacks(event_type(agent=agent))


def _agent() -> Agent:
    return Agent(model=FakeModel(), callback_handler=None)


def test_a_masked_agent_writes_back_a_masked_copy_and_the_run_keeps_it_whole() -> None:
    exchange = HistoryExchange()
    set_history_exchange(exchange)
    agent = _agent()
    agent.messages = _call(1)

    _fire(HistoryHook("k", enabled=True, policy=MASKED), agent, AfterInvocationEvent)

    assert exchange.outbound["k"] == _call(1)
    assert exchange.written_back()["k"] == _masked_call(1)


def test_a_second_call_in_the_same_run_is_seeded_with_the_whole_transcript() -> None:
    set_history_exchange(HistoryExchange())
    hook = HistoryHook("k", enabled=True, policy=MASKED)
    first = _agent()
    first.messages = _call(1)
    _fire(hook, first, AfterInvocationEvent)

    second = _agent()
    _fire(hook, second, BeforeInvocationEvent)

    assert second.messages == _call(1)


def test_a_memory_stored_in_full_is_masked_when_a_masked_agent_loads_it() -> None:
    set_history_exchange(HistoryExchange(inbound={"k": [*_call(1), *_call(2)]}))
    agent = _agent()

    _fire(HistoryHook("k", enabled=True, policy=MASKED), agent, BeforeInvocationEvent)

    assert agent.messages == [*_masked_call(1), *_masked_call(2)]


def test_a_full_history_is_loaded_and_written_back_as_it_is() -> None:
    exchange = HistoryExchange(inbound={"k": _call(1)}, persisted={"k": _masked_call(1)})
    set_history_exchange(exchange)
    hook = HistoryHook("k", enabled=True)
    agent = _agent()

    _fire(hook, agent, BeforeInvocationEvent)
    _fire(hook, agent, AfterInvocationEvent)

    assert agent.messages == _call(1)
    assert exchange.written_back()["k"] == _call(1)
