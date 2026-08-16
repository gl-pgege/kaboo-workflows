"""Push an eval report to Langfuse as a dataset experiment (optional extra).

Uploads the golden items to a Langfuse dataset (idempotent upsert by item id),
then records one experiment run per report: each item becomes a trace linked
to its dataset item, with every scorer verdict attached as a score. Requires
the ``langfuse`` extra and ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` /
``LANGFUSE_BASE_URL`` (or ``LANGFUSE_HOST``) in the environment.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runner import EvalReport

logger = logging.getLogger(__name__)


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

    client.create_dataset(name=report.dataset)
    for outcome in report.outcomes:
        client.create_dataset_item(
            dataset_name=report.dataset,
            id=outcome.item.id,
            input=outcome.item.input,
            metadata=outcome.item.metadata or None,
        )

    dataset = client.get_dataset(report.dataset)
    items_by_id = {item.id: item for item in dataset.items}
    for outcome in report.outcomes:
        dataset_item = items_by_id.get(outcome.item.id)
        if dataset_item is None:
            logger.warning("dataset item %s missing after upsert; skipping", outcome.item.id)
            continue
        with dataset_item.run(run_name=run) as span:
            span.update_trace(
                input=outcome.item.input,
                output=outcome.capture.text,
                metadata={
                    "tools": outcome.capture.tool_names(),
                    "usage": outcome.capture.usage,
                    "latency_s": outcome.capture.latency_s,
                    "error": outcome.capture.error,
                },
            )
            for score in outcome.scores:
                span.score_trace(
                    name=score.scorer,
                    value=score.score if score.score is not None else (1 if score.passed else 0),
                    comment=score.details,
                )

    client.flush()
    return run
