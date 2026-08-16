"""DeepEval metric adapter (optional ``eval-deepeval`` extra).

Wraps any DeepEval metric as a kaboo eval scorer, so datasets can use the
DeepEval library's catalog (answer relevancy, faithfulness, GEval rubrics, …)
alongside the built-ins::

    expect:
      - type: deepeval
        metric: deepeval.metrics:AnswerRelevancyMetric
        params:
          threshold: 0.7
          model: gpt-4.1-mini
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..utils import load_object
from .scorers import ScoreResult

if TYPE_CHECKING:
    from .capture import RunCapture
    from .dataset import EvalItem


class DeepEvalScorer:
    """Score a run with a DeepEval metric."""

    def __init__(
        self,
        metric: str,
        params: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> None:
        """Configure the adapter.

        Args:
            metric: Import spec of the metric class, e.g.
                ``deepeval.metrics:AnswerRelevancyMetric``.
            params: Constructor kwargs for the metric (threshold, model, …).
            name: Result label; defaults to the metric class name.
        """
        try:
            import deepeval  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "the deepeval scorer requires the eval-deepeval extra: "
                "pip install 'kaboo-workflows[eval-deepeval]'"
            ) from exc
        metric_cls = load_object(metric, target="DeepEval metric")
        self._metric = metric_cls(**(params or {}))
        self.name = name or type(self._metric).__name__

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Run the metric against a DeepEval test case built from the capture."""
        from deepeval.test_case import LLMTestCase

        test_case = LLMTestCase(
            input=item.input,
            actual_output=capture.text,
            expected_output=item.metadata.get("expected_output"),
            context=item.metadata.get("context"),
        )
        try:
            await self._metric.a_measure(test_case)
        except Exception as exc:
            return ScoreResult(self.name, False, None, f"deepeval metric failed: {exc}")
        score = float(self._metric.score) if self._metric.score is not None else None
        passed = bool(self._metric.success)
        return ScoreResult(self.name, passed, score, getattr(self._metric, "reason", None))
