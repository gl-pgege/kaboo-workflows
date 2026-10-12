"""What a model is sent, against what the client keeps.

kaboo rewrites the copy of the conversation each model call is sent and never
the conversation itself. These drive the production adapter path and check both
sides: the scripted model's input, and the ``MESSAGES_SNAPSHOT`` and
``kaboo_history`` a host would persist and replay.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ag_ui.core import AssistantMessage, FunctionCall, ToolCall, ToolMessage, UserMessage

from kaboo_workflows.testing import Pipeline, ScriptedModel
from tests.fakes import LocalBedrockModel

CONFIGS = Path(__file__).parent / "configs"


def _blocks(messages: list[dict[str, Any]], kind: str) -> list[Any]:
    return [b[kind] for m in messages for b in m["content"] if kind in b]


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name, arguments=json.dumps(arguments)))


def _turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The conversation alone, without the bookkeeping keys strands adds to messages."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


async def test_a_foreign_call_in_the_chat_reaches_the_model_as_a_note_and_the_client_keeps_it():
    pipe = Pipeline(str(CONFIGS / "foreign_calls.yaml"))
    chat = pipe.infra.models["chat"]
    assert isinstance(chat, ScriptedModel)
    transcript = [
        UserMessage(id="u1", content="how much revenue?"),
        AssistantMessage(id="a1", tool_calls=[_call("tc-sql", "analyze_sql", {"input": "2024"})]),
        ToolMessage(id="t1", tool_call_id="tc-sql", content="Revenue in 2024 was $42M."),
        AssistantMessage(
            id="a2", tool_calls=[_call("tc-report", "submit_report", {"title": "Q4"})]
        ),
        ToolMessage(id="t2", tool_call_id="tc-report", content="report 'Q4' submitted"),
        AssistantMessage(id="a3", content="Revenue was $42M and the report is filed."),
        UserMessage(id="u2", content="and in 2023?"),
    ]

    r = await pipe.turn(thread_id="f1", run_id="r1", messages=transcript)

    assert not r.errored()
    sent = chat.calls[0]
    assert [u["toolUseId"] for u in _blocks(sent, "toolUse")] == ["tc-report"]
    assert [res["toolUseId"] for res in _blocks(sent, "toolResult")] == ["tc-report"]
    assert any(
        text.startswith("Earlier step (analyze_sql, a tool you don't have)")
        for text in _blocks(sent, "text")
    )

    persisted = r.messages()
    calls = {
        tc.id: tc.function.name
        for m in persisted
        if m.role == "assistant"
        for tc in (getattr(m, "tool_calls", None) or [])
    }
    assert calls["tc-sql"] == "analyze_sql"
    assert calls["tc-report"] == "submit_report"
    assert {"tc-sql", "tc-report"} <= {m.tool_call_id for m in persisted if m.role == "tool"}


async def test_a_foreign_call_in_loaded_memory_reaches_the_model_as_a_note_and_is_kept():
    pipe = Pipeline(str(CONFIGS / "foreign_calls.yaml"))
    keeper = pipe.infra.models["keeper"]
    assert isinstance(keeper, ScriptedModel)
    memory = [
        {"role": "user", "content": [{"text": "what was 2024 revenue?"}]},
        {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "toolUseId": "m1",
                        "name": "athena___run_query",
                        "input": {"sql": "select sum(revenue) from sales"},
                    }
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "toolResult": {
                        "toolUseId": "m1",
                        "status": "success",
                        "content": [{"text": '{"total": 42000000}'}],
                    }
                }
            ],
        },
        {"role": "assistant", "content": [{"text": "2024 revenue was $42M."}]},
    ]

    r = await pipe.turn(
        "and in 2023?", thread_id="f2", run_id="r1", state={"kaboo_history": {"finance": memory}}
    )

    assert not r.errored()
    sent = keeper.calls[-1]
    assert _blocks(sent, "toolUse") == []
    assert _blocks(sent, "toolResult") == []
    assert sent[1]["content"][0]["text"].startswith(
        "Earlier step (athena___run_query, a tool you don't have)"
    )
    assert _turns(r.state()["kaboo_history"]["finance"][: len(memory)]) == memory


async def test_a_bedrock_sub_agent_caches_its_loaded_memory_without_writing_the_point_back():
    pipe = Pipeline(str(CONFIGS / "memory_cache_point.yaml"))
    claude = pipe.infra.models["claude"]
    assert isinstance(claude, LocalBedrockModel)
    memory = [
        {"role": "user", "content": [{"text": "what was 2024 revenue?"}]},
        {"role": "assistant", "content": [{"text": "It was $42M."}]},
    ]

    r = await pipe.turn(
        "and 2022?", thread_id="c1", run_id="r1", state={"kaboo_history": {"finance": memory}}
    )

    assert not r.errored()
    assert claude.calls[-1][1]["content"] == [
        {"text": "It was $42M."},
        {"cachePoint": {"type": "default"}},
    ]
    written_back = r.state()["kaboo_history"]["finance"]
    assert not [b for m in written_back for b in m["content"] if "cachePoint" in b]
