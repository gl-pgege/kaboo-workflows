"""End-to-end approvals that tools ask for themselves (``interrupt.tool_approvals``).

A tool whose own service enforces approvals replies ``kaboo_approval`` instead of
running. Driven through real agents (``ScriptedModel``) and the ``Pipeline``
harness. The tests play the host: they tell the fake service about each decision
before resuming, as kaboo-runtime's ``onDecision`` does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from ag_ui.core import EventType as AGUIEventType

from kaboo_workflows.testing import Pipeline, ScriptedModel
from tests.fakes.enforcing_tools import SERVICE

CONFIGS = Path(__file__).parent / "configs"
TOOL = str(CONFIGS / "tool_approval.yaml")
STORES = str(CONFIGS / "tool_approval_stores.yaml")
OFF = str(CONFIGS / "tool_approval_off.yaml")
DELEGATE = str(CONFIGS / "tool_approval_delegate.yaml")
SUMMIT = {"store": "summit", "sku": "A1", "price": 10}


@pytest.fixture(autouse=True)
def _fresh_service() -> Iterator[None]:
    SERVICE.reset()
    yield
    SERVICE.reset()


def _waiting(result: Any) -> list[Any]:
    outcome = result.run_outcome()
    assert getattr(outcome, "type", None) == "interrupt", "the run did not pause"
    return list(outcome.interrupts)


def _answer(interrupt: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"interruptId": interrupt.id, "status": "resolved", "payload": payload}]


def _results(result: Any) -> list[str]:
    return [str(e.content) for e in result.of_type(AGUIEventType.TOOL_CALL_RESULT)]


async def test_a_tool_reply_pauses_the_run_with_the_tools_own_request() -> None:
    r1 = await Pipeline(TOOL).turn("raise the price", thread_id="t1", run_id="r1")

    (waiting,) = _waiting(r1)
    reason = waiting.metadata
    assert reason["type"] == "approval"
    assert reason["request"] == "req-1"
    assert reason["subject"] == {
        "key": "store:summit/catalog",
        "label": "catalog changes in Summit",
    }
    assert reason["scopes"] == ["once", "thread"]
    assert reason["message"] == "Set the price of A1 to 10.0"
    assert reason["tool_input"] == SUMMIT
    assert SERVICE.calls == []
    published = r1.state()["kaboo_session"]["approvals"]
    assert [(a["interruptId"], a["request"]) for a in published] == [(waiting.id, "req-1")]


@pytest.mark.parametrize("reply", ["text", "json", "structured"])
async def test_each_reply_shape_pauses_the_run(reply: str) -> None:
    SERVICE.reply = reply

    r1 = await Pipeline(TOOL).turn("raise the price", thread_id="t2", run_id="r1")

    assert [w.metadata["request"] for w in _waiting(r1)] == ["req-1"]
    assert SERVICE.calls == []


async def test_approving_once_calls_the_tool_exactly_once_more() -> None:
    pipe = Pipeline(TOOL)
    (waiting,) = _waiting(await pipe.turn("raise the price", thread_id="t3", run_id="r1"))
    SERVICE.decide("req-1")

    r2 = await pipe.turn(
        thread_id="t3", run_id="r2", resume=_answer(waiting, {"status": "approved"})
    )

    assert not r2.errored() and r2.finished()
    assert SERVICE.invocations == 2
    assert SERVICE.calls == [SUMMIT]
    assert any("A1 now costs 10.0" in r for r in _results(r2))


async def test_the_person_can_edit_the_call_before_approving_it() -> None:
    pipe = Pipeline(TOOL)
    (waiting,) = _waiting(await pipe.turn("raise the price", thread_id="t4", run_id="r1"))
    edited = {**SUMMIT, "price": 12}
    SERVICE.decide("req-1", call=edited)

    await pipe.turn(
        thread_id="t4",
        run_id="r2",
        resume=_answer(waiting, {"status": "approved", "tool_input": edited}),
    )

    assert SERVICE.calls == [edited]


@pytest.mark.parametrize(
    ("entry", "note"),
    [
        (
            {"status": "resolved", "payload": {"status": "cancelled", "reason": "too low"}},
            "Declined: too low",
        ),
        ({"status": "cancelled"}, "Skipped:"),
    ],
)
async def test_a_denial_or_a_skip_never_calls_the_tool_again(
    entry: dict[str, Any], note: str
) -> None:
    pipe = Pipeline(TOOL)
    (waiting,) = _waiting(await pipe.turn("raise the price", thread_id="t5", run_id="r1"))

    r2 = await pipe.turn(thread_id="t5", run_id="r2", resume=[{"interruptId": waiting.id, **entry}])

    assert not r2.errored() and r2.finished()
    assert SERVICE.invocations == 1
    assert SERVICE.calls == []
    assert any(note in r for r in _results(r2))


async def test_an_approval_the_service_never_heard_of_does_not_loop() -> None:
    pipe = Pipeline(TOOL)
    (waiting,) = _waiting(await pipe.turn("raise the price", thread_id="t6", run_id="r1"))

    r2 = await pipe.turn(
        thread_id="t6", run_id="r2", resume=_answer(waiting, {"status": "approved"})
    )

    assert not r2.errored() and r2.finished()
    assert SERVICE.invocations == 2
    assert SERVICE.calls == []
    assert any("Still needs approval" in r for r in _results(r2))


async def test_a_decision_for_the_chat_covers_only_the_store_it_named() -> None:
    pipe = Pipeline(STORES)
    r1 = await pipe.turn("raise both prices", thread_id="t7", run_id="r1")
    waiting = {w.metadata["subject"]["key"]: w for w in _waiting(r1)}
    summit, north = waiting["store:summit/catalog"], waiting["store:north/catalog"]
    SERVICE.decide(summit.metadata["request"], scope="thread")
    SERVICE.decide(north.metadata["request"])

    await pipe.turn(
        thread_id="t7",
        run_id="r2",
        resume=[
            *_answer(summit, {"status": "approved", "scope": "thread"}),
            *_answer(north, {"status": "approved"}),
        ],
    )
    r3 = await pipe.turn("and once more", thread_id="t7", run_id="r3")

    assert sorted(c["store"] for c in SERVICE.calls[:2]) == ["north", "summit"]
    assert [c["store"] for c in SERVICE.calls[2:]] == ["summit"]
    assert [w.metadata["subject"]["key"] for w in _waiting(r3)] == ["store:north/catalog"]


async def test_the_reply_never_reaches_the_model_and_a_forged_request_changes_nothing() -> None:
    pipe = Pipeline(TOOL)
    chat = pipe.infra.models["chat"]
    assert isinstance(chat, ScriptedModel)
    (waiting,) = _waiting(await pipe.turn("raise the price", thread_id="t8", run_id="r1"))
    SERVICE.decide("req-1")

    r2 = await pipe.turn(
        thread_id="t8",
        run_id="r2",
        resume=_answer(waiting, {"status": "approved", "request": "req-forged"}),
    )

    sent = [str(block) for call in chat.calls for message in call for block in message["content"]]
    assert sent and not any("kaboo_approval" in block for block in sent)
    assert not any("kaboo_approval" in r for r in _results(r2))
    assert "req-forged" not in json.dumps(r2.state())
    assert SERVICE.calls == [SUMMIT]


async def test_a_sub_agents_decision_is_recorded_on_its_tool_activity_entry() -> None:
    pipe = Pipeline(DELEGATE)
    (waiting,) = _waiting(await pipe.turn("raise the price", thread_id="t10", run_id="r1"))
    assert waiting.metadata["request"] == "req-1"
    SERVICE.decide("req-1")

    r2 = await pipe.turn(
        thread_id="t10", run_id="r2", resume=_answer(waiting, {"status": "approved"})
    )

    assert SERVICE.calls == [SUMMIT]
    pricer = next(g for g in r2.groups.values() if g["agentName"] == "pricer")
    tool = next(t for t in pricer["tools"] if t["toolName"] == "set_price")
    assert tool["approval"] == {"decision": "approved", "by": "person", "scope": "once"}


async def test_with_the_flag_off_the_reply_reaches_the_model_and_nothing_runs() -> None:
    r1 = await Pipeline(OFF).turn("raise the price", thread_id="t9", run_id="r1")

    assert r1.finished() and getattr(r1.run_outcome(), "type", None) != "interrupt"
    assert SERVICE.calls == []
    assert any("kaboo_approval" in r for r in _results(r1))
