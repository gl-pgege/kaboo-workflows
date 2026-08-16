"""Golden dataset loading (YAML or JSONL).

A dataset is a list of items, each an input message plus its expectations
(scorer configs). YAML form::

    name: golden-v1
    config: ./config.yaml          # optional; --config / run_eval(config=) wins
    scorers:                       # applied to every item
      - type: budget
        max_cost: 2.0
    items:
      - id: revenue
        input: "What was total revenue last quarter?"
        expect:
          - type: contains
            value: revenue
          - type: judge
            rubric: Gives a concrete revenue number with its source.
            model: judge

JSONL form: one item object per line (``{"id": ..., "input": ..., "expect":
[...]}``); the dataset name defaults to the file stem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EvalItem:
    """One golden dataset item: an input and its expectations."""

    id: str
    input: str
    expect: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] | None = None
    forwarded_props: dict[str, Any] | None = None
    timeout_s: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalDataset:
    """A named collection of :class:`EvalItem` plus dataset-wide scorers."""

    name: str
    items: list[EvalItem]
    scorers: list[dict[str, Any]] = field(default_factory=list)
    config: str | None = None


def _parse_item(raw: dict[str, Any], index: int) -> EvalItem:
    if not isinstance(raw, dict):
        raise ValueError(f"dataset item #{index} must be a mapping, got {type(raw).__name__}")
    item_id = str(raw.get("id") or f"item-{index}")
    text = raw.get("input")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"dataset item '{item_id}' needs a non-empty string 'input'")
    expect = raw.get("expect") or []
    if not isinstance(expect, list):
        raise ValueError(f"dataset item '{item_id}': 'expect' must be a list of scorer configs")
    timeout = raw.get("timeout_s")
    return EvalItem(
        id=item_id,
        input=text,
        expect=[dict(e) for e in expect],
        state=raw.get("state") if isinstance(raw.get("state"), dict) else None,
        forwarded_props=(
            raw.get("forwarded_props") if isinstance(raw.get("forwarded_props"), dict) else None
        ),
        timeout_s=float(timeout) if timeout is not None else None,
        metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
    )


def load_eval_dataset(path: str | Path) -> EvalDataset:
    """Load a golden dataset from a ``.yaml``/``.yml`` or ``.jsonl`` file.

    Args:
        path: Dataset file path.

    Returns:
        The parsed :class:`EvalDataset`.

    Raises:
        FileNotFoundError: The file doesn't exist.
        ValueError: The file is structurally invalid.
    """
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"dataset file not found: {file}")

    if file.suffix == ".jsonl":
        items = [
            _parse_item(json.loads(line), i)
            for i, line in enumerate(file.read_text().splitlines())
            if line.strip()
        ]
        return EvalDataset(name=file.stem, items=items)

    raw = yaml.safe_load(file.read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError(f"dataset {file} must be a mapping with an 'items' list")
    items = [_parse_item(entry, i) for i, entry in enumerate(raw["items"])]
    scorers = raw.get("scorers") or []
    if not isinstance(scorers, list):
        raise ValueError(f"dataset {file}: 'scorers' must be a list of scorer configs")
    config = raw.get("config")
    if config is not None:
        # Relative config paths resolve against the dataset file, so datasets
        # are runnable from any working directory.
        config = str((file.parent / str(config)).resolve())
    return EvalDataset(
        name=str(raw.get("name") or file.stem),
        items=items,
        scorers=[dict(s) for s in scorers],
        config=config,
    )
