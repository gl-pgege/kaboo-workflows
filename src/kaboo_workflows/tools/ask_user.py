"""Built-in ``ask_user`` tool for proactive human-in-the-loop questions.

The tool lets an agent ask the user one or more questions at any point during
execution. It supports single questions (radio, checkbox, or text) and
multi-question forms rendered in the frontend.

The tool raises a native strands interrupt from *inside* its body via
``tool_context.interrupt()``: the first call pauses the agent and surfaces the
question(s) in the UI; on resume the same call returns the user's answer, which
the tool returns as its result so the model can act on it.
"""

from __future__ import annotations

import json
from typing import Any

from strands import tool
from strands.types.tools import ToolContext


def _format_answer(response: Any) -> str:
    """Serialise the user's resume payload as the tool result.

    The payload (a ``{question: answer}`` map, or a bare value) is emitted as
    JSON so it is the single source of truth for both consumers: the model reads
    it as its answer, and the frontend parses it to render the answered Q&A card
    inline. Non-answers (cancel / no response) return a short readable note the
    frontend treats as "no answer".
    """
    if response is None:
        return "The user did not provide an answer."
    if isinstance(response, dict) and response.get("status") == "cancelled":
        return "The user declined to answer."
    return json.dumps(response, ensure_ascii=False)


@tool(name="ask_user", context=True)
def ask_user(
    tool_context: ToolContext,
    question: str | None = None,
    options: list[str] | None = None,
    input_type: str | None = None,
    questions: list[dict] | None = None,
) -> dict:
    """Ask the user one or more questions and wait for their answer.

    For a single question, use ``question`` + optional ``options``:
      ask_user(question="Which market?", options=["AI", "Cloud", "IoT"])
      ask_user(question="What should I focus on?")

    For multiple questions at once, use ``questions``:
      ask_user(questions=[
        {"question": "Which market?", "type": "radio", "options": ["AI", "Cloud"]},
        {"question": "Select sectors", "type": "checkbox", "options": ["B2B", "B2C"]},
        {"question": "Any context?", "type": "text"}
      ])

    Args:
        question: Single question text (shorthand).
        options: Options for single question. Omit for free text.
        input_type: "radio", "checkbox", or "text" for single question. When
            omitted, defaults to "radio" if options are given, else "text".
        questions: List of question dicts for multi-question forms.
    """
    form_questions = questions
    if form_questions is None:
        q_type = input_type or ("radio" if options else "text")
        form_questions = [{"question": question or "", "type": q_type, "options": options}]

    response = tool_context.interrupt(
        name="ask_user",
        reason={"type": "form", "questions": form_questions},
    )

    return {"status": "success", "content": [{"text": _format_answer(response)}]}
