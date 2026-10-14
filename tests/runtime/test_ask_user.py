"""The built-in ``ask_user`` tool asks multiple-choice questions only."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from strands.types.tools import ToolContext

from kaboo_workflows.tools import ask_user


class _Paused(Exception):
    """Stands in for strands' InterruptException: the run pauses here."""


class _ToolContext:
    """Stands in for strands' ToolContext: records the request, pauses or answers."""

    def __init__(self, answer: Any = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self._answer = answer

    def interrupt(self, name: str, reason: dict[str, Any]) -> Any:
        self.requests.append(reason)
        if self._answer is None:
            raise _Paused
        return self._answer


def _ask(context: _ToolContext, **kwargs: Any) -> dict[str, Any]:
    try:
        return ask_user(tool_context=cast(ToolContext, context), **kwargs)
    except _Paused:
        return {"status": "paused"}


def _questions(context: _ToolContext) -> list[dict[str, Any]]:
    return context.requests[0]["questions"]


def test_a_question_with_options_pauses_for_the_answer() -> None:
    context = _ToolContext()
    result = _ask(context, question="Which market?", options=["AI", "Cloud"])

    assert result == {"status": "paused"}
    assert context.requests[0]["type"] == "form"
    assert _questions(context) == [
        {
            "question": "Which market?",
            "options": ["AI", "Cloud"],
            "multiple": False,
            "type": "radio",
        }
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"question": "What should I focus on?"},
        {"question": "Which market?", "options": ["AI"]},
        {"question": "Which market?", "options": ["Other"]},
        {"question": "", "options": ["AI", "Cloud"]},
        {"question": "Anything else?", "input_type": "text"},
        {
            "questions": [
                {"question": "Which market?", "options": ["AI", "Cloud"]},
                {"question": "Why?"},
            ]
        },
        {"questions": ["Which market?"]},
        {"questions": []},
    ],
)
def test_a_question_that_is_not_multiple_choice_is_refused_without_asking(
    kwargs: dict[str, Any],
) -> None:
    context = _ToolContext()
    result = _ask(context, **kwargs)

    assert result["status"] == "error"
    assert context.requests == []


def test_other_blanks_and_duplicates_are_dropped_from_the_options() -> None:
    context = _ToolContext()
    _ask(
        context,
        question="Which countries?",
        options=["US", "us", " ", "Other", "Other (please specify)", "Other countries", "Canada"],
    )
    assert _questions(context)[0]["options"] == ["US", "Other countries", "Canada"]


def test_multiple_makes_a_multi_select_question() -> None:
    context = _ToolContext()
    _ask(context, question="Which sectors?", options=["B2B", "B2C"], multiple=True)

    question = _questions(context)[0]
    assert question["multiple"] is True
    assert question["type"] == "checkbox"


@pytest.mark.parametrize(("legacy", "multiple"), [("radio", False), ("checkbox", True)])
def test_the_deprecated_input_type_still_works(legacy: str, multiple: bool) -> None:
    context = _ToolContext()
    _ask(context, question="Which sectors?", options=["B2B", "B2C"], input_type=legacy)
    assert _questions(context)[0]["multiple"] is multiple


def test_several_questions_are_asked_together() -> None:
    context = _ToolContext()
    _ask(
        context,
        questions=[
            {"question": "Which market?", "options": ["AI", "Cloud"]},
            {"question": "Which sectors?", "type": "checkbox", "options": ["B2B", "B2C"]},
        ],
    )
    assert [(q["question"], q["multiple"]) for q in _questions(context)] == [
        ("Which market?", False),
        ("Which sectors?", True),
    ]


def test_the_answers_reach_the_model_as_json() -> None:
    answer = {"Which market?": "Robotics", "Which sectors?": ["B2B", "B2C"]}
    result = _ask(_ToolContext(answer), question="Which market?", options=["AI", "Cloud"])

    assert result["status"] == "success"
    assert json.loads(result["content"][0]["text"]) == answer


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "cancelled", "skipped": True},
        {"status": "cancelled", "expired": True},
        {"status": "cancelled"},
    ],
)
def test_a_skipped_question_answers_with_a_note_not_an_answer(answer: dict[str, Any]) -> None:
    result = _ask(_ToolContext(answer), question="Which market?", options=["AI", "Cloud"])

    text = result["content"][0]["text"]
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)
