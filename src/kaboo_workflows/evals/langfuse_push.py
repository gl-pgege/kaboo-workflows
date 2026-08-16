"""Push an eval report to Langfuse as a dataset experiment (optional extra).

Uploads the golden items to a Langfuse dataset (idempotent upsert by item id),
then records one experiment run per report via the v4 ``run_experiment`` API:
each item is replayed as a trace linked to its dataset item, with every scorer
verdict attached as a score. Requires the ``langfuse`` extra and
``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_BASE_URL``
(or ``LANGFUSE_HOST``) in the environment.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runner import EvalReport, ItemOutcome

logger = logging.getLogger(__name__)


def _ensure_dataset(client: Any, name: str) -> None:
    """Create *name* if it does not already exist."""
    try:
        client.get_dataset(name)
        return
    except Exception:
        logger.debug("dataset %s not found; creating", name)
    client.create_dataset(name=name)


def _replay_task(outcomes_by_id: dict[str, ItemOutcome]):
    """Return already-captured output for a Langfuse dataset item."""

    def task(*, item: Any, **_kwargs: Any) -> dict[str, Any]:
        outcome = outcomes_by_id[item.id]
        return {
            "item_id": outcome.item.id,
            "text": outcome.capture.text,
            "tools": outcome.capture.tool_names(),
            "usage": outcome.capture.usage,
            "latency_s": outcome.capture.latency_s,
            "error": outcome.capture.error,
        }

    return task


def _replay_scores(outcomes_by_id: dict[str, ItemOutcome]):
    """Attach stored scorer verdicts as Langfuse evaluations."""

    def evaluator(*, output: Any, **_kwargs: Any) -> list[Any]:
        from langfuse import Evaluation

        item_id = output.get("item_id") if isinstance(output, dict) else None
        outcome = outcomes_by_id.get(item_id) if item_id else None
        if outcome is None:
            return []
        return [
            Evaluation(
                name=score.scorer,
                value=score.score if score.score is not None else (1.0 if score.passed else 0.0),
                comment=score.details,
            )
            for score in outcome.scores
        ]

    return evaluator


def push_report(report: EvalReport, *, run_name: str | None = None) -> str:
    """Upload *report* to Langfuse as a dataset experiment run.

    Args:
        report: A finished :class:`~kaboo_workflows.evals.runner.EvalReport`.
        run_name: Experiment run name; defaults to ``eval-<unix-ts>``.

    Returns:
        The run name used (visible under the dataset's experiments in the UI).

    Raises:
        ImportError: The ``langfuse`` package is not installed.
    """
    try:
        from langfuse import get_client
    except ImportError as exc:
        raise ImportError(
            "pushing to Langfuse requires the langfuse extra: "
            "pip install 'kaboo-workflows[langfuse]'"
        ) from exc

    if not os.getenv("LANGFUSE_HOST") and os.getenv("LANGFUSE_BASE_URL"):
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]

    client = get_client()
    run = run_name or f"eval-{int(time.time())}"

    _ensure_dataset(client, report.dataset)
    for outcome in report.outcomes:
        client.create_dataset_item(
            dataset_name=report.dataset,
            id=outcome.item.id,
            input=outcome.item.input,
            metadata=outcome.item.metadata or None,
        )

    dataset = client.get_dataset(report.dataset)
    outcomes_by_id = {outcome.item.id: outcome for outcome in report.outcomes}
    dataset.items = [item for item in dataset.items if item.id in outcomes_by_id]
    if not dataset.items:
        logger.warning("no dataset items matched report %s; skipping experiment", report.dataset)
        client.flush()
        return run

    result = dataset.run_experiment(
        name=run,
        run_name=run,
        description="kaboo-workflows eval",
        task=_replay_task(outcomes_by_id),
        evaluators=[_replay_scores(outcomes_by_id)],
        metadata={"source": "kaboo-workflows.eval"},
    )
    client.flush()
    return result.run_name or run
