"""Langfuse experiment upload uses the v4 observation + dataset-run-item APIs."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from kaboo_workflows.evals.capture import RunCapture
from kaboo_workflows.evals.dataset import EvalItem
from kaboo_workflows.evals.langfuse_push import push_report
from kaboo_workflows.evals.runner import EvalReport, ItemOutcome
from kaboo_workflows.evals.scorers import ScoreResult


def test_push_report_creates_dataset_run_without_item_run(monkeypatch):
    span = MagicMock()
    span.trace_id = "trace-1"
    span.id = "obs-1"
    span_cm = MagicMock()
    span_cm.__enter__.return_value = span
    span_cm.__exit__.return_value = False

    dataset_item = SimpleNamespace(id="greet")
    client = MagicMock()
    client.get_dataset.return_value = SimpleNamespace(items=[dataset_item])
    client.start_as_current_observation.return_value = span_cm

    fake_langfuse = SimpleNamespace(get_client=lambda: client)
    monkeypatch.setitem(sys.modules, "langfuse", fake_langfuse)

    report = EvalReport(
        dataset="plain-golden",
        outcomes=[
            ItemOutcome(
                item=EvalItem(id="greet", input="hi", metadata={"k": "v"}),
                capture=RunCapture(
                    item_id="greet",
                    text="hello",
                    usage={"totalTokens": 3},
                    latency_s=0.4,
                ),
                scores=[ScoreResult("contains", True, 1.0, None)],
            )
        ],
    )

    run = push_report(report, run_name="eval-test")

    assert run == "eval-test"
    client.create_dataset_item.assert_called_once()
    client.start_as_current_observation.assert_called_once()
    span.score_trace.assert_called_once()
    client.api.dataset_run_items.create.assert_called_once_with(
        run_name="eval-test",
        dataset_item_id="greet",
        trace_id="trace-1",
        observation_id="obs-1",
    )
    client.flush.assert_called_once()
