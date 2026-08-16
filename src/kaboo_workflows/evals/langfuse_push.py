"""Push an eval report to Langfuse as a dataset experiment (optional extra).

Uploads the golden items to a Langfuse dataset (idempotent upsert by item id),
then records one experiment run per report: each item becomes a trace linked
to its dataset item, with every scorer verdict attached as a score. Requires
the ``langfuse`` extra and ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` /
``LANGFUSE_BASE_URL`` (or ``LANGFUSE_HOST``) in the environment.

Compatible with Langfuse Python SDK v3 and v4 (v4 dropped ``DatasetItem.run``).
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runner import EvalReport

logger = logging.getLogger(__name__)


def _ensure_dataset(client: Any, name: str) -> None:
    """Create *name* if it does not already exist."""
    try:
        client.get_dataset(name)
        return
    except Exception:
        logger.debug("dataset %s not found; creating", name)
    client.create_dataset(name=name)


def _link_run_item(client: Any, *, run: str, dataset_item_id: str, trace_id: str, observation_id: str) -> None:
    """Attach a trace to a dataset experiment run (Langfuse v4 REST)."""
    client.api.dataset_run_items.create(
        run_name=run,
        dataset_item_id=dataset_item_id,
        trace_id=trace_id,
        observation_id=observation_id,
    )


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
    items_by_id = {item.id: item for item in dataset.items}
    for outcome in report.outcomes:
        dataset_item = items_by_id.get(outcome.item.id)
        if dataset_item is None:
            logger.warning("dataset item %s missing after upsert; skipping", outcome.item.id)
            continue
        metadata = {
            "tools": outcome.capture.tool_names(),
            "usage": outcome.capture.usage,
            "latency_s": outcome.capture.latency_s,
            "error": outcome.capture.error,
        }
        with client.start_as_current_observation(
            name=f"eval:{outcome.item.id}",
            as_type="span",
            input=outcome.item.input,
            output=outcome.capture.text,
            metadata=metadata,
        ) as span:
            span.update(
                input=outcome.item.input,
                output=outcome.capture.text,
                metadata=metadata,
            )
            for score in outcome.scores:
                span.score_trace(
                    name=score.scorer,
                    value=score.score if score.score is not None else (1 if score.passed else 0),
                    comment=score.details,
                )
            _link_run_item(
                client,
                run=run,
                dataset_item_id=dataset_item.id,
                trace_id=span.trace_id,
                observation_id=span.id,
            )

    client.flush()
    return run
