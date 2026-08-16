"""Deterministic scorer built-ins and the build_scorer dispatch."""

from __future__ import annotations

import pytest

from kaboo_workflows.evals.capture import RunCapture, ToolInvocation
from kaboo_workflows.evals.dataset import EvalItem
from kaboo_workflows.evals.judge import JudgeScorer, _extract_json
from kaboo_workflows.evals.scorers import RunSucceededScorer, build_scorer

ITEM = EvalItem(id="i1", input="question")


def capture(**overrides) -> RunCapture:
    defaults = dict(item_id="i1", text="Total revenue was $1,234 in Q3.")
    defaults.update(overrides)
    return RunCapture(**defaults)


async def test_contains_passes_and_reports_missing():
    scorer = build_scorer({"type": "contains", "value": ["revenue", "$1,234"]})
    assert (await scorer.score(ITEM, capture())).passed
    result = await scorer.score(ITEM, capture(text="no numbers here"))
    assert not result.passed
    assert "missing" in (result.details or "")


async def test_contains_case_sensitivity():
    insensitive = build_scorer({"type": "contains", "value": "REVENUE"})
    assert (await insensitive.score(ITEM, capture())).passed
    sensitive = build_scorer({"type": "contains", "value": "REVENUE", "case_sensitive": True})
    assert not (await sensitive.score(ITEM, capture())).passed


async def test_not_contains():
    scorer = build_scorer({"type": "not_contains", "value": "error"})
    assert (await scorer.score(ITEM, capture())).passed
    assert not (await scorer.score(ITEM, capture(text="an error occurred"))).passed


async def test_regex():
    scorer = build_scorer({"type": "regex", "pattern": r"\$[\d,]+"})
    assert (await scorer.score(ITEM, capture())).passed
    assert not (await scorer.score(ITEM, capture(text="no currency"))).passed


async def test_tool_called_with_agent_scope_and_count():
    tools = [
        ToolInvocation(agent="analyst", name="run_sql", status="success"),
        ToolInvocation(agent="analyst", name="run_sql", status="success"),
        ToolInvocation(agent="viz", name="render_chart", status="success"),
    ]
    cap = capture(tools=tools)
    assert (await build_scorer({"type": "tool_called", "tool": "run_sql"}).score(ITEM, cap)).passed
    assert (
        await build_scorer(
            {"type": "tool_called", "tool": "run_sql", "agent": "analyst", "min_count": 2}
        ).score(ITEM, cap)
    ).passed
    result = await build_scorer({"type": "tool_called", "tool": "run_sql", "agent": "viz"}).score(
        ITEM, cap
    )
    assert not result.passed


async def test_trajectory_subsequence():
    tools = [
        ToolInvocation(agent="a", name="plan", status="success"),
        ToolInvocation(agent="a", name="run_sql", status="success"),
        ToolInvocation(agent="a", name="summarize", status="success"),
    ]
    cap = capture(tools=tools)
    ordered = build_scorer({"type": "trajectory", "tools": ["plan", "summarize"]})
    assert (await ordered.score(ITEM, cap)).passed
    reversed_ = build_scorer({"type": "trajectory", "tools": ["summarize", "plan"]})
    result = await reversed_.score(ITEM, cap)
    assert not result.passed
    assert result.score is not None and result.score < 1.0


async def test_budget_caps():
    cap = capture(usage={"cost": 0.5, "totalTokens": 1000}, latency_s=12.0)
    ok = build_scorer({"type": "budget", "max_cost": 1.0, "max_latency_s": 60})
    assert (await ok.score(ITEM, cap)).passed
    over = build_scorer({"type": "budget", "max_cost": 0.1, "max_total_tokens": 500})
    result = await over.score(ITEM, cap)
    assert not result.passed
    assert "cost" in (result.details or "") and "totalTokens" in (result.details or "")


async def test_json_schema_on_structured_output():
    schema = {"type": "object", "required": ["total"], "properties": {"total": {"type": "number"}}}
    scorer = build_scorer({"type": "json_schema", "schema": schema})
    assert (await scorer.score(ITEM, capture(structured_outputs=[{"total": 5}]))).passed
    assert not (await scorer.score(ITEM, capture(structured_outputs=[{"nope": 1}]))).passed
    # Falls back to parsing the final text as JSON.
    assert (await scorer.score(ITEM, capture(text='{"total": 9}'))).passed
    assert not (await scorer.score(ITEM, capture(text="not json"))).passed


async def test_run_succeeded_reflects_capture_error():
    scorer = RunSucceededScorer()
    assert (await scorer.score(ITEM, capture())).passed
    result = await scorer.score(ITEM, capture(error="boom"))
    assert not result.passed and result.details == "boom"


def test_unknown_type_rejected():
    with pytest.raises(ValueError, match="unknown scorer type"):
        build_scorer({"type": "nonsense"})


def test_missing_type_rejected():
    with pytest.raises(ValueError, match="needs a 'type'"):
        build_scorer({"value": "x"})


async def test_custom_scorer_via_import_spec(tmp_path):
    custom = tmp_path / "custom_scorer.py"
    custom.write_text(
        """
from kaboo_workflows.evals.scorers import ScoreResult

class AlwaysPass:
    name = "always"
    def __init__(self, label="ok"):
        self.label = label
    async def score(self, item, capture):
        return ScoreResult(self.name, True, 1.0, self.label)
"""
    )
    scorer = build_scorer({"type": f"{custom}:AlwaysPass", "label": "hi"})
    result = await scorer.score(ITEM, capture())
    assert result.passed and result.details == "hi"


# --- judge ------------------------------------------------------------------


def test_extract_json_variants():
    assert _extract_json('{"score": 0.5}') == {"score": 0.5}
    assert _extract_json('Verdict: {"score": 1.0, "reasoning": "x"} done') == {
        "score": 1.0,
        "reasoning": "x",
    }
    assert _extract_json("no json at all") is None


def _judge(final_text: str, threshold: float = 0.7) -> JudgeScorer:
    return JudgeScorer(
        rubric="Answers with a number.",
        model={
            "provider": "tests.fakes.scripted_model:ScriptedModel",
            "model_id": "scripted",
            "params": {"final_text": final_text},
        },
        threshold=threshold,
    )


async def test_judge_passes_above_threshold():
    result = await _judge('{"score": 0.9, "reasoning": "solid"}').score(ITEM, capture())
    assert result.passed and result.score == 0.9
    assert "solid" in (result.details or "")


async def test_judge_fails_below_threshold():
    result = await _judge('{"score": 0.2, "reasoning": "weak"}').score(ITEM, capture())
    assert not result.passed and result.score == 0.2


async def test_judge_tolerates_garbage_verdict():
    result = await _judge("I refuse to answer in JSON").score(ITEM, capture())
    assert not result.passed and result.score is None


def test_judge_requires_model_source():
    with pytest.raises(ValueError, match="needs the workflow config"):
        JudgeScorer(rubric="r", model="judge", app_config=None)
