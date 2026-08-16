"""Golden dataset parsing (YAML and JSONL)."""

from __future__ import annotations

import json

import pytest

from kaboo_workflows.evals import load_eval_dataset


def test_yaml_dataset_parses_items_and_defaults(tmp_path):
    file = tmp_path / "golden.yaml"
    file.write_text(
        """
name: golden-v1
config: ./config.yaml
scorers:
  - type: budget
    max_cost: 2.0
items:
  - id: revenue
    input: "What was revenue?"
    expect:
      - type: contains
        value: revenue
  - input: "Second question"
"""
    )
    ds = load_eval_dataset(file)
    assert ds.name == "golden-v1"
    assert ds.config == str((tmp_path / "config.yaml").resolve())
    assert ds.scorers == [{"type": "budget", "max_cost": 2.0}]
    assert [i.id for i in ds.items] == ["revenue", "item-1"]
    assert ds.items[0].expect == [{"type": "contains", "value": "revenue"}]


def test_jsonl_dataset_named_after_file(tmp_path):
    file = tmp_path / "smoke.jsonl"
    lines = [
        {"id": "a", "input": "q1", "expect": [{"type": "regex", "pattern": "x"}]},
        {"id": "b", "input": "q2"},
    ]
    file.write_text("\n".join(json.dumps(entry) for entry in lines))
    ds = load_eval_dataset(file)
    assert ds.name == "smoke"
    assert [i.id for i in ds.items] == ["a", "b"]
    assert ds.items[0].expect[0]["type"] == "regex"


def test_missing_input_rejected(tmp_path):
    file = tmp_path / "bad.yaml"
    file.write_text("items:\n  - id: no-input\n")
    with pytest.raises(ValueError, match="non-empty string 'input'"):
        load_eval_dataset(file)


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_eval_dataset("/nope/never.yaml")
