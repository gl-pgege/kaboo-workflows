"""Langfuse experiment upload uses the v4 run_experiment API."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from kaboo_workflows.evals.capture import RunCapture
from kaboo_workflows.evals.dataset import EvalItem
from kaboo_workflows.evals.langfuse_push import push_report
from kaboo_workflows.evals.runner import EvalReport, ItemOutcome
from kaboo_workflows.evals.scorers import ScoreResult


def test_push_report_replays_via_run_experiment(monkeypatch):
    extra = SimpleNamespace(id="other")
    dataset_item = SimpleNamespace(id="greet")
    dataset = SimpleNamespace(items=[extra, dataset_item], run_experiment=MagicMock())
    dataset.run_experiment.return_value = SimpleNamespace(run_name="eval-test")

    client = MagicMock()
    client.get_dataset.return_value = dataset

    fake_langfuse = SimpleNamespace(get_client=lambda: client, Evaluation=object)
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
    dataset.run_experiment.assert_called_once()
    kwargs = dataset.run_experiment.call_args.kwargs
    assert kwargs["name"] == "eval-test"
    assert kwargs["run_name"] == "eval-test"
    assert [item.id for item in dataset.items] == ["greet"]
    replayed = kwargs["task"](item=dataset_item)
    assert replayed["item_id"] == "greet"
    assert replayed["text"] == "hello"
    client.flush.assert_called_once()
