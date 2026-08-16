"""Offline evaluation for kaboo workflows.

Run a golden dataset headlessly through the same wire path as production,
score the outcomes with pluggable scorers (deterministic checks, budgets,
LLM-as-judge, DeepEval adapters), and gate on the result::

    kaboo-workflows eval datasets/golden.yaml --config config.yaml

or from pytest::

    from kaboo_workflows.evals import run_eval, assert_eval


    async def test_golden():
        report = await run_eval("datasets/golden.yaml", config="config.yaml")
        assert_eval(report)

Heavy dependencies stay optional: the judge uses the config's own ``models``
section, DeepEval ships behind the ``eval-deepeval`` extra, and Langfuse
dataset-run uploads behind the ``langfuse`` extra.
"""

from .capture import EvalPipeline, RunCapture
from .dataset import EvalDataset, EvalItem, load_eval_dataset
from .runner import EvalReport, ItemOutcome, assert_eval, run_eval
from .scorers import ScoreResult, Scorer, build_scorer

__all__ = [
    "EvalDataset",
    "EvalItem",
    "EvalPipeline",
    "EvalReport",
    "ItemOutcome",
    "RunCapture",
    "ScoreResult",
    "Scorer",
    "assert_eval",
    "build_scorer",
    "load_eval_dataset",
    "run_eval",
]
