"""Calls to tools the running agent lacks reach its model as text notes.

Only the copy of the conversation sent to the model changes; the agent's own
messages keep the original calls, which is what the chat and the captured
history are built from.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from strands import Agent, tool

from kaboo_workflows.hooks import ContextHook
from kaboo_workflows.hooks._transcript import foreign_calls_to_notes
from tests.fakes import LocalBedrockModel, ScriptedModel


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"text": text}]}


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": list(blocks)}


def _use(tool_use_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}}


def _result(tool_use_id: str, text: str) -> dict[str, Any]:
    return {
        "toolResult": {"toolUseId": tool_use_id, "status": "success", "content": [{"text": text}]}
    }


def _note(name: str, asked: str, returned: str) -> dict[str, Any]:
    return {
        "text": f"Earlier step ({name}, a tool you don't have): asked {asked}; returned {returned}"
    }


def _turns(messages: list[Any]) -> list[dict[str, Any]]:
    """The conversation alone, without the bookkeeping keys strands adds to messages."""
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _assert_bedrock_valid(messages: list[dict[str, Any]]) -> None:
    roles = [m["role"] for m in messages]
    assert roles[0] == "user"
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False))
    assert all(m["content"] for m in messages)
    previous: dict[str, Any] = {"content": []}
    for message in messages:
        asked = {b["toolUse"]["toolUseId"] for b in previous["content"] if "toolUse" in b}
        answered = {b["toolResult"]["toolUseId"] for b in message["content"] if "toolResult" in b}
        assert answered <= asked
        previous = message


def test_a_call_to_a_tool_the_agent_lacks_becomes_a_note_carrying_its_result() -> None:
    transcript = [
        _user("how much revenue?"),
        _assistant({"text": "Asking the analyst."}, _use("t1", "analyze_sql", {"input": "2024"})),
        {"role": "user", "content": [_result("t1", "Revenue in 2024 was $42M.")]},
        _assistant({"text": "Revenue was $42M."}),
        _user("and in 2023?"),
    ]

    notes = foreign_calls_to_notes(transcript, tools={"query"})

    assert notes == [
        _user("how much revenue?"),
        _assistant(
            {"text": "Asking the analyst."},
            _note("analyze_sql", '{"input": "2024"}', "Revenue in 2024 was $42M."),
            {"text": "Revenue was $42M."},
        ),
        _user("and in 2023?"),
    ]
    _assert_bedrock_valid(notes)


def test_calls_to_tools_the_agent_has_are_left_alone() -> None:
    transcript = [
        _user("q"),
        _assistant(_use("t1", "query", {"sql": "select 1"})),
        {"role": "user", "content": [_result("t1", "1")]},
        _assistant({"text": "one"}),
    ]

    assert foreign_calls_to_notes(transcript, tools={"query"}) == transcript


def test_a_note_goes_before_the_calls_that_stay_and_their_results_stay_paired() -> None:
    transcript = [
        _user("q"),
        _assistant(_use("own", "query", {"sql": "s"}), _use("gone", "analyze_sql", {"input": "i"})),
        {"role": "user", "content": [_result("own", "rows"), _result("gone", "report")]},
        _assistant({"text": "done"}),
    ]

    notes = foreign_calls_to_notes(transcript, tools={"query"})

    assert notes[1] == _assistant(
        _note("analyze_sql", '{"input": "i"}', "report"), _use("own", "query", {"sql": "s"})
    )
    assert notes[2] == {"role": "user", "content": [_result("own", "rows")]}
    _assert_bedrock_valid(notes)


def test_an_mcp_note_keeps_500_input_characters_and_an_excerpt_of_the_result() -> None:
    tool_input = {"sql": "x" * 2000}
    transcript = [
        _user("q"),
        _assistant(_use("t1", "athena___run_query", tool_input)),
        {"role": "user", "content": [_result("t1", "r" * 3000)]},
        _assistant({"text": "done"}),
    ]

    notes = foreign_calls_to_notes(transcript, tools=set())

    asked = json.dumps(tool_input)[:500] + "…"
    returned = "r" * 500 + "… [3,000 characters in full]"
    assert notes[1]["content"][0] == _note("athena___run_query", asked, returned)


def test_a_delegate_note_keeps_2000_input_characters_and_its_whole_result() -> None:
    tool_input = {"input": "q" * 5000}
    transcript = [
        _user("q"),
        _assistant(_use("t1", "analyze_sql", tool_input)),
        {"role": "user", "content": [_result("t1", "a" * 3000)]},
        _assistant({"text": "done"}),
    ]

    notes = foreign_calls_to_notes(transcript, tools=set())

    assert notes[1]["content"][0] == _note(
        "analyze_sql", json.dumps(tool_input)[:2000] + "…", "a" * 3000
    )


def test_a_foreign_call_left_without_a_result_still_becomes_a_note() -> None:
    transcript = [
        _user("q"),
        _assistant({"text": "checking"}, _use("t1", "analyze_sql", {"input": "i"})),
        _user("never mind"),
    ]

    notes = foreign_calls_to_notes(transcript, tools=set())

    assert notes[1]["content"][1] == _note("analyze_sql", '{"input": "i"}', "no result")
    _assert_bedrock_valid(notes)


def test_a_call_answered_in_the_last_message_is_left_alone() -> None:
    transcript = [
        _user("q"),
        _assistant(_use("t1", "code-workbench___update_query", {"query_id": "q1"})),
        {"role": "user", "content": [_result("t1", "Error: unknown tool")]},
    ]

    assert foreign_calls_to_notes(transcript, tools={"query"}) == transcript


def test_only_the_calls_the_agent_inherited_become_notes() -> None:
    transcript = [
        _user("how much revenue?"),
        _assistant(_use("t1", "analyze_sql", {"input": "2024"})),
        {"role": "user", "content": [_result("t1", "Revenue in 2024 was $42M.")]},
        _assistant({"text": "Revenue was $42M."}),
        _user("and in 2023?"),
        _assistant(_use("t2", "athena___run_query", {"sql": "s"})),
        {"role": "user", "content": [_result("t2", "Error: unknown tool")]},
        _assistant({"text": "Trying the right tool."}),
        _user("go on"),
    ]

    notes = foreign_calls_to_notes(transcript, tools={"query"}, inherited=5)

    assert notes[1]["content"][0] == _note(
        "analyze_sql", '{"input": "2024"}', "Revenue in 2024 was $42M."
    )
    assert notes[3] == _assistant(_use("t2", "athena___run_query", {"sql": "s"}))
    _assert_bedrock_valid(notes)


def test_the_transcript_passed_in_is_not_changed() -> None:
    transcript = [
        _user("q"),
        _assistant(_use("t1", "analyze_sql", {"input": "i"})),
        {"role": "user", "content": [_result("t1", "report")]},
        _assistant({"text": "done"}),
    ]
    before = copy.deepcopy(transcript)

    foreign_calls_to_notes(transcript, tools=set())

    assert transcript == before


@tool
def query(sql: str) -> str:
    """Run a SQL query."""
    return "rows"


async def test_the_model_is_sent_notes_while_the_agent_keeps_the_original_calls() -> None:
    history: list[Any] = [
        _user("how much revenue?"),
        _assistant(_use("t1", "analyze_sql", {"input": "2024"})),
        {"role": "user", "content": [_result("t1", "Revenue in 2024 was $42M.")]},
        _assistant({"text": "Revenue was $42M."}),
    ]
    model = ScriptedModel(final_text="In 2023 it was $40M.")
    agent = Agent(
        model=model,
        tools=[query],
        hooks=[ContextHook()],
        messages=copy.deepcopy(history),
        callback_handler=None,
    )

    await agent.invoke_async("and in 2023?")

    sent = model.calls[-1]
    assert not any("toolUse" in b or "toolResult" in b for m in sent for b in m["content"])
    assert (
        _note("analyze_sql", '{"input": "2024"}', "Revenue in 2024 was $42M.") in sent[1]["content"]
    )
    assert _turns(agent.messages[: len(history)]) == history


async def test_a_mistyped_tool_reaches_the_model_as_the_call_and_its_error() -> None:
    model = LocalBedrockModel(tool="code-workbench___update_query")
    agent = Agent(model=model, tools=[query], hooks=[ContextHook()], callback_handler=None)

    await agent.invoke_async("update the query")

    sent = model.calls[-1]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[1]["content"][-1]["toolUse"]["name"] == "code-workbench___update_query"
    assert sent[2]["content"][0]["toolResult"]["status"] == "error"
    _assert_bedrock_valid(sent)
