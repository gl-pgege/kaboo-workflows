"""A masked memory over ``summarize_over`` folds its oldest calls into a summary.

The summary pair leads the memory, the newest calls that fit stay as they were,
and every ``To remember`` block of a summarised report is carried into the
summary verbatim. A summary that fails leaves the masked memory as it is.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from strands import Agent
from strands.hooks import HookRegistry
from strands.hooks.events import AfterInvocationEvent
from strands.models import Model

from kaboo_workflows._context import HistoryExchange, set_history_exchange
from kaboo_workflows.hooks import HistoryHook, HistoryPolicy
from kaboo_workflows.hooks._transcript import mask_transcript, to_remember_blocks
from tests.fakes import BoomModel, FakeModel, ScriptedModel

SUMMARY_BRIEF = "Summary of your earlier work in this conversation"
REMEMBER = "## To remember\n- warehouse id wh-7\n- revenue is stored in cents"
SUMMARY = "Queries q-1, q-2 and q-3 found $42M of revenue; returns are still open."


def _call(n: int, report: str) -> list[Any]:
    rows = json.dumps({"queryExecutionId": f"q-{n}", "padding": "x" * 2000})
    use = {"toolUseId": f"c{n}", "name": "athena___run_query", "input": {"n": n}}
    result = {"toolUseId": f"c{n}", "status": "success", "content": [{"text": rows}]}
    return [
        {"role": "user", "content": [{"text": f"brief {n}"}]},
        {"role": "assistant", "content": [{"toolUse": use}]},
        {"role": "user", "content": [{"toolResult": result}]},
        {"role": "assistant", "content": [{"text": report}]},
    ]


def _masked(messages: list[Any]) -> list[Any]:
    return mask_transcript(messages, min_chars=1500, kept=set(), tools=set())


def _four_calls() -> list[Any]:
    return [
        *_call(1, f"Report 1.\n\n{REMEMBER}\n\n## Next steps\n- check returns"),
        *_call(2, "Report 2."),
        *_call(3, "Report 3."),
        *_call(4, "Report 4."),
    ]


@pytest.fixture(autouse=True)
def _isolate_exchange() -> Iterator[None]:
    set_history_exchange(None)
    yield
    set_history_exchange(None)


async def _capture(messages: list[Any], summarizer: Model, *, over: int = 200) -> list[Any]:
    """Capture ``messages`` with a summarising masked policy; return the memory written back."""
    exchange = HistoryExchange()
    set_history_exchange(exchange)
    policy = HistoryPolicy(keep="masked", summarize_over=over, summarizer=summarizer)
    registry = HookRegistry()
    registry.add_hook(HistoryHook("k", enabled=True, policy=policy))
    agent = Agent(model=FakeModel(), callback_handler=None)
    agent.messages = messages

    await registry.invoke_callbacks_async(AfterInvocationEvent(agent=agent))

    return exchange.written_back()["k"]


async def test_the_oldest_calls_fold_into_a_leading_summary_pair_and_the_newest_stay() -> None:
    memory = await _capture(_four_calls(), ScriptedModel(final_text=SUMMARY))

    assert memory[0] == {"role": "user", "content": [{"text": SUMMARY_BRIEF}]}
    assert memory[1] == {"role": "assistant", "content": [{"text": SUMMARY}, {"text": REMEMBER}]}
    assert memory[2:] == _masked(_call(4, "Report 4."))


async def test_the_summarizer_is_sent_the_ids_and_told_to_keep_them() -> None:
    summarizer = ScriptedModel(final_text=SUMMARY)

    await _capture(_four_calls(), summarizer)

    prompt = summarizer.calls[-1][0]["content"][0]["text"]
    for n in (1, 2, 3):
        assert f"queryExecutionId=q-{n}" in prompt
        assert f"Report {n}." in prompt
    assert "queryExecutionId=q-4" not in prompt
    instructions = summarizer.system_prompts[-1]
    for kept in ("ID", "decision", "exact figure", "open issue"):
        assert kept in instructions


async def test_a_to_remember_block_survives_a_second_summary_verbatim() -> None:
    first = await _capture(_four_calls(), ScriptedModel(final_text=SUMMARY))

    second = await _capture(
        [*first, *_call(5, "Report 5."), *_call(6, "Report 6.")],
        ScriptedModel(final_text="Everything up to q-5."),
    )

    assert second[0] == {"role": "user", "content": [{"text": SUMMARY_BRIEF}]}
    assert second[1]["content"] == [{"text": "Everything up to q-5."}, {"text": REMEMBER}]


async def test_a_failed_summary_writes_back_the_masked_memory() -> None:
    memory = await _capture(_four_calls(), BoomModel())

    assert memory == _masked(_four_calls())


async def test_a_memory_within_its_budget_is_not_summarised() -> None:
    summarizer = ScriptedModel(final_text=SUMMARY)

    memory = await _capture(_four_calls(), summarizer, over=100_000)

    assert summarizer.calls == []
    assert memory == _masked(_four_calls())


@pytest.mark.parametrize(
    ("report", "blocks"),
    [
        pytest.param(
            "Done.\n\n## To remember\n- a\n- b\n\n## Next steps\n- c",
            ["## To remember\n- a\n- b"],
            id="heading-to-next-heading",
        ),
        pytest.param(
            "## To remember\n### Ids\n- wh-7\n## Other\n- x",
            ["## To remember\n### Ids\n- wh-7"],
            id="keeps-its-subheadings",
        ),
        pytest.param(
            "Done.\nTo remember: invoice inv-9 is disputed.\nIt was raised in May.\n# Appendix",
            ["To remember: invoice inv-9 is disputed.\nIt was raised in May."],
            id="plain-line-to-next-heading",
        ),
        pytest.param("**To remember**\n- keep me", ["**To remember**\n- keep me"], id="bold-line"),
        pytest.param("Nothing worth noting to remember.", [], id="mid-line-is-not-a-block"),
    ],
)
def test_to_remember_blocks_are_found_verbatim(report: str, blocks: list[str]) -> None:
    assert to_remember_blocks(report) == blocks
