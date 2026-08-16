"""Scorer protocol and deterministic built-ins.

A scorer config is a mapping with a ``type`` key. Built-in types:

- ``contains`` / ``not_contains`` — substring check on the final text
  (``value``: string or list; ``case_sensitive``: default false)
- ``regex`` — ``pattern`` must match the final text
- ``tool_called`` — a tool named ``name`` ran (``agent`` and ``min_count``
  optional)
- ``trajectory`` — ``tools`` (list) appear in order as a subsequence of the
  run's tool calls
- ``budget`` — caps on ``max_cost`` ($), ``max_latency_s``,
  ``max_input_tokens``, ``max_output_tokens``, ``max_total_tokens``
- ``json_schema`` — a structured output validates against ``schema``
  (requires the ``jsonschema`` package)
- ``judge`` — LLM-as-judge (see :mod:`kaboo_workflows.evals.judge`)
- ``deepeval`` — a DeepEval metric (requires the ``eval-deepeval`` extra)

Any other ``type`` containing a colon is resolved via
:func:`~kaboo_workflows.utils.load_object` (``module.path:ClassName`` or
``./file.py:ClassName``) — the same plugin mechanism as model providers — and
constructed with the remaining config keys as kwargs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..utils import load_object

if TYPE_CHECKING:
    from ..config.schema import AppConfig
    from .capture import RunCapture
    from .dataset import EvalItem


@dataclass
class ScoreResult:
    """One scorer's verdict on one item."""

    scorer: str
    passed: bool
    score: float | None = None
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for report lines."""
        return {
            "scorer": self.scorer,
            "passed": self.passed,
            "score": self.score,
            "details": self.details,
        }


@runtime_checkable
class Scorer(Protocol):
    """Anything that can score a captured run against an item's expectations."""

    name: str

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Return this scorer's verdict for *item* given *capture*."""
        ...


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise ValueError(f"expected a string or list of strings, got {type(value).__name__}")


class ContainsScorer:
    """Final text contains every expected substring."""

    def __init__(self, value: Any, case_sensitive: bool = False, negate: bool = False) -> None:
        """Configure with expected substring(s)."""
        self._expected = _texts(value)
        self._case_sensitive = case_sensitive
        self._negate = negate
        self.name = "not_contains" if negate else "contains"

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Check each expected substring against the final text."""
        haystack = capture.text if self._case_sensitive else capture.text.lower()
        missing = []
        found = []
        for needle in self._expected:
            probe = needle if self._case_sensitive else needle.lower()
            (found if probe in haystack else missing).append(needle)
        if self._negate:
            passed = not found
            details = f"unexpectedly found: {found}" if found else None
        else:
            passed = not missing
            details = f"missing: {missing}" if missing else None
        return ScoreResult(self.name, passed, 1.0 if passed else 0.0, details)


class RegexScorer:
    """Final text matches a regular expression."""

    name = "regex"

    def __init__(self, pattern: str, flags: str = "") -> None:
        """Compile *pattern* (flags: any of ``imsx``)."""
        re_flags = 0
        for ch in flags:
            re_flags |= {
                "i": re.IGNORECASE,
                "m": re.MULTILINE,
                "s": re.DOTALL,
                "x": re.VERBOSE,
            }[ch]
        self._pattern = re.compile(pattern, re_flags)

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Search the final text for the pattern."""
        passed = self._pattern.search(capture.text) is not None
        details = None if passed else f"pattern {self._pattern.pattern!r} not found"
        return ScoreResult(self.name, passed, 1.0 if passed else 0.0, details)


class ToolCalledScorer:
    """A named tool ran during the run."""

    name = "tool_called"

    def __init__(self, tool: str, agent: str | None = None, min_count: int = 1) -> None:
        """Configure with the tool name, optional agent scope, and count."""
        self._tool = tool
        self._agent = agent
        self._min_count = min_count

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Count matching tool invocations."""
        count = sum(
            1
            for t in capture.tools
            if t.name == self._tool and (self._agent is None or t.agent == self._agent)
        )
        passed = count >= self._min_count
        scope = f" by {self._agent}" if self._agent else ""
        details = None if passed else f"{self._tool}{scope} ran {count}x, needed {self._min_count}"
        return ScoreResult(self.name, passed, 1.0 if passed else 0.0, details)


class TrajectoryScorer:
    """Expected tools appear in order (as a subsequence of the actual calls)."""

    name = "trajectory"

    def __init__(self, tools: list[str]) -> None:
        """Configure with the ordered tool-name subsequence."""
        self._tools = list(tools)

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Match the expected subsequence against the actual call order."""
        actual = capture.tool_names()
        it = iter(actual)
        matched = sum(1 for expected in self._tools if any(expected == got for got in it))
        passed = matched == len(self._tools)
        details = (
            None
            if passed
            else f"matched {matched}/{len(self._tools)} of {self._tools}; actual order: {actual}"
        )
        score = matched / len(self._tools) if self._tools else 1.0
        return ScoreResult(self.name, passed, score, details)


class BudgetScorer:
    """Run cost / latency / token caps."""

    name = "budget"

    def __init__(
        self,
        max_cost: float | None = None,
        max_latency_s: float | None = None,
        max_input_tokens: int | None = None,
        max_output_tokens: int | None = None,
        max_total_tokens: int | None = None,
    ) -> None:
        """Configure with any subset of caps; unset caps are not checked."""
        self._caps = {
            "cost": max_cost,
            "latency_s": max_latency_s,
            "inputTokens": max_input_tokens,
            "outputTokens": max_output_tokens,
            "totalTokens": max_total_tokens,
        }

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Compare actuals against every configured cap."""
        actuals = {**capture.usage, "latency_s": capture.latency_s}
        breaches = []
        for key, cap in self._caps.items():
            if cap is None:
                continue
            actual = actuals.get(key, 0) or 0
            if actual > cap:
                breaches.append(f"{key}={actual} > {cap}")
        passed = not breaches
        return ScoreResult(self.name, passed, 1.0 if passed else 0.0, "; ".join(breaches) or None)


class JsonSchemaScorer:
    """A structured output (or the text parsed as JSON) validates against a schema."""

    name = "json_schema"

    def __init__(self, schema: dict[str, Any]) -> None:
        """Configure with a JSON Schema mapping (requires ``jsonschema``)."""
        try:
            import jsonschema
        except ImportError as exc:
            raise ImportError(
                "the json_schema scorer requires the 'jsonschema' package: pip install jsonschema"
            ) from exc
        self._validator = jsonschema.Draft202012Validator(schema)

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Validate structured outputs, falling back to text parsed as JSON."""
        candidates: list[Any] = list(capture.structured_outputs)
        if not candidates:
            try:
                candidates.append(json.loads(capture.text))
            except (ValueError, TypeError):
                return ScoreResult(
                    self.name, False, 0.0, "no structured output and text is not JSON"
                )
        errors: list[str] = []
        for candidate in candidates:
            found = [e.message for e in self._validator.iter_errors(candidate)]
            if not found:
                return ScoreResult(self.name, True, 1.0)
            errors.extend(found)
        return ScoreResult(self.name, False, 0.0, "; ".join(errors[:5]))


class RunSucceededScorer:
    """The run finished without an error (implicitly checked for every item)."""

    name = "run_succeeded"

    async def score(self, item: EvalItem, capture: RunCapture) -> ScoreResult:
        """Fail when the capture recorded a run error."""
        passed = capture.error is None
        return ScoreResult(self.name, passed, 1.0 if passed else 0.0, capture.error)


def build_scorer(config: dict[str, Any], app_config: AppConfig | None = None) -> Scorer:
    """Construct a scorer from its config mapping.

    Args:
        config: The scorer config (``type`` plus type-specific keys).
        app_config: The workflow config, needed by the ``judge`` scorer to
            resolve its model from the ``models:`` section.

    Returns:
        A ready :class:`Scorer`.

    Raises:
        ValueError: Unknown ``type`` or invalid parameters.
    """
    params = {k: v for k, v in config.items() if k != "type"}
    kind = config.get("type")
    if not isinstance(kind, str) or not kind:
        raise ValueError(f"scorer config needs a 'type': {config!r}")

    match kind:
        case "contains":
            return ContainsScorer(**params)
        case "not_contains":
            return ContainsScorer(**params, negate=True)
        case "regex":
            return RegexScorer(**params)
        case "tool_called":
            return ToolCalledScorer(**params)
        case "trajectory":
            return TrajectoryScorer(**params)
        case "budget":
            return BudgetScorer(**params)
        case "json_schema":
            return JsonSchemaScorer(**params)
        case "judge":
            from .judge import JudgeScorer

            return JudgeScorer(app_config=app_config, **params)
        case "deepeval":
            from .deepeval_adapter import DeepEvalScorer

            return DeepEvalScorer(**params)
        case _ if ":" in kind:
            cls = load_object(kind, target="eval scorer")
            scorer = cls(**params)
            if not hasattr(scorer, "score"):
                raise ValueError(f"custom scorer {kind} has no score() method")
            return scorer
        case _:
            raise ValueError(
                f"unknown scorer type '{kind}'. Built-ins: contains, not_contains, regex, "
                "tool_called, trajectory, budget, json_schema, judge, deepeval; or use "
                "'module.path:ClassName' for a custom scorer."
            )
