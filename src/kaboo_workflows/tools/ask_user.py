"""Built-in ``ask_user`` tool for proactive human-in-the-loop questions.

The tool lets an agent ask the person one or more multiple-choice questions at
any point during execution. Every question offers two or more options, single
or multi-select, and the person can always pick Other and answer in their own
words — the card adds that choice, so the agent never lists it. Free-text
questions are refused with a tool error that teaches the rule, so the model
fixes its call without bothering the person.

The tool raises a native strands interrupt from *inside* its body via
``tool_context.interrupt()``: the first call pauses the agent and surfaces the
question(s) in the UI; on resume the same call returns the person's answer,
which the tool returns as its result so the model can act on it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from strands import tool
from strands.types.tools import ToolContext

logger = logging.getLogger(__name__)

_RULE = (
    "Ask multiple-choice questions only: give every question at least two short, "
    "distinct options, set multiple to true when several can apply, and do not add an "
    "'Other' option, because the person can always answer in their own words."
)

_OTHER = re.compile(r"other(\s*\(.*\))?[.:]?")


def _clean_options(raw: Any) -> list[str]:
    """Strip blanks, duplicates and any "Other" choice from the model's options."""
    if not isinstance(raw, list):
        return []
    options: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item).strip() if item is not None else ""
        folded = text.casefold()
        if not text or folded in seen or _OTHER.fullmatch(folded):
            continue
        seen.add(folded)
        options.append(text)
    return options


def _form_questions(raw: list[Any]) -> tuple[list[dict[str, Any]], str | None]:
    """Normalise the model's questions, or say why they cannot be asked.

    Returns:
        The questions for the form, and ``None``; or an empty list and the
        reason the call was refused.
    """
    questions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            return [], f"Each question must be an object with question and options. {_RULE}"
        text = str(item.get("question") or "").strip()
        if not text:
            return [], f"Each question needs its text. {_RULE}"
        legacy = item.get("type")
        if legacy == "text":
            return [], f"Free-text questions are not supported. {_RULE}"
        if legacy in ("radio", "checkbox"):
            logger.warning("type=<%s> | ask_user question type is deprecated; use multiple", legacy)
        multiple = bool(item.get("multiple")) or legacy == "checkbox"
        options = _clean_options(item.get("options"))
        if len(options) < 2:
            return [], f"'{text}' needs at least two options. {_RULE}"
        questions.append(
            {
                "question": text,
                "options": options,
                "multiple": multiple,
                "type": "checkbox" if multiple else "radio",
            }
        )
    if not questions:
        return [], _RULE
    return questions, None


def _format_answer(response: Any) -> str:
    """Serialise the person's resume payload as the tool result.

    The payload (a ``{question: answer}`` map, with a list for a multi-select
    question and the person's own words for Other) is emitted as JSON so it is
    the single source of truth for both consumers: the model reads it as its
    answer, and the frontend parses it to render the answered Q&A card inline.
    A skip, an expiry or a decline returns a short note the frontend treats as
    "no answer".
    """
    if response is None:
        return "The person did not answer."
    if isinstance(response, dict) and response.get("status") == "cancelled":
        if response.get("skipped") or response.get("expired"):
            return "The person skipped this question."
        return "The person declined to answer."
    return json.dumps(response, ensure_ascii=False)


@tool(name="ask_user", context=True)
def ask_user(
    tool_context: ToolContext,
    question: str | None = None,
    options: list[str] | None = None,
    multiple: bool = False,
    questions: list[dict] | None = None,
    input_type: str | None = None,
) -> dict:
    """Ask the person multiple-choice questions and wait for their answers.

    Use this whenever you need the person's input or a decision, instead of
    asking in your reply. Every question needs at least two short, distinct
    options. Set multiple to true when several options can apply. Never add an
    "Other" option: the person can always choose Other and type their own
    answer, which you receive as their words.

    One question:
      ask_user(question="Which market?", options=["AI", "Cloud", "IoT"])

    Several questions at once:
      ask_user(questions=[
        {"question": "Which market?", "options": ["AI", "Cloud"]},
        {"question": "Which sectors?", "options": ["B2B", "B2C"], "multiple": true}
      ])

    Args:
        question: The question, when asking one.
        options: Its options, at least two.
        multiple: Whether the person may choose several options.
        questions: Several questions at once, each {question, options, multiple}.
        input_type: Deprecated; use multiple. "checkbox" means multiple.
    """
    if questions is not None:
        raw: list[Any] = list(questions)
    else:
        raw = [{"question": question, "options": options, "multiple": multiple, "type": input_type}]
    form_questions, refusal = _form_questions(raw)
    if refusal is not None:
        return {"status": "error", "content": [{"text": refusal}]}

    response = tool_context.interrupt(
        name="ask_user",
        reason={"type": "form", "questions": form_questions},
    )

    return {"status": "success", "content": [{"text": _format_answer(response)}]}
