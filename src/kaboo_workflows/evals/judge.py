"""LLM-as-judge scorer.

Scores a captured run against a natural-language rubric using a model from
the workflow config's own ``models:`` section — no separate eval-model
plumbing. The judge sees the user input, the final answer, and the tool
trajectory, and returns a 0..1 score with reasoning.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from .scorers import ScoreResult

if TYPE_CHECKING:
    from ..config.schema import AppConfig
    from .capture import RunCapture
    from .dataset import EvalItem

_SYSTEM_PROMPT = """You are an impartial evaluator of AI assistant answers.
Judge the answer strictly against the rubric. Respond with ONLY a JSON object:
{"score": <float 0.0-1.0>, "reasoning": "<one or two sentences>"}
Score 1.0 = fully satisfies the rubric; 0.0 = not at all. No other text."""

_MAX_ANSWER_CHARS = 8000


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            return None
    return None


class JudgeScorer:
    """Score a run against a rubric with an LLM from the config's ``models:``."""

    def __init__(
        self,
        rubric: str,
        model: str | dict[str, Any] | None = None,
        threshold: float = 0.7,
        app_config: AppConfig | None = None,
        name: str = "judge",
    ) -> None:
        """Configure the judge.

        Args:
            rubric: What a good answer must do, in plain language.
            model: A model name from the config's ``models:`` section, an
                inline model mapping (``{provider, model_id, params}``), or
                ``None`` to use the config's ``default`` model.
            threshold: Minimum score (0..1) to pass.
            app_config: The workflow config (injected by ``build_scorer``).
            name: Result label, useful when an item has several judges.
        """
        from ..config.schema import ModelDef

        self.name = name
        self._rubric = rubric
        self._threshold = threshold

        if isinstance(model, dict):
            self._model_def = ModelDef.model_validate(model)
        else:
            if app_config is None:
                raise ValueError(
                    "judge scorer needs the workflow config to resolve a named model; "
                    "pass an inline model mapping instead"
                )
            model_name = model or "default"
            model_def = app_config.models.get(model_name)
            if model_def is None:
                raise ValueError(
                    f"judge model '{model_name}' is not defined under models:. "
                    f"Available: {', '.join(sorted(app_config.models)) or '(none)'}"
                )
            self._model_def = model_def

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Ask the judge model to score the captured answer."""
        from strands import Agent

        from ..config.resolvers.models import resolve_model

        answer = capture.text[:_MAX_ANSWER_CHARS]
        trajectory = ", ".join(capture.tool_names()) or "(none)"
        prompt = (
            f"Rubric:\n{self._rubric}\n\n"
            f"User input:\n{item.input}\n\n"
            f"Tools the assistant used (in order): {trajectory}\n\n"
            f"Assistant answer:\n{answer or '(empty answer)'}"
        )

        agent = Agent(
            model=resolve_model(self._model_def),
            system_prompt=_SYSTEM_PROMPT,
            callback_handler=None,
        )
        try:
            result = await agent.invoke_async(prompt)
        except Exception as exc:
            return ScoreResult(self.name, False, None, f"judge call failed: {exc}")

        parsed = _extract_json(str(result))
        if parsed is None or not isinstance(parsed.get("score"), (int, float)):
            return ScoreResult(
                self.name, False, None, f"judge returned unparseable verdict: {str(result)[:200]}"
            )
        score = max(0.0, min(1.0, float(parsed["score"])))
        reasoning = str(parsed.get("reasoning", ""))
        passed = score >= self._threshold
        details = f"score {score:.2f} (threshold {self._threshold}): {reasoning}"
        return ScoreResult(self.name, passed, score, details)
