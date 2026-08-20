"""Eval orchestration: dataset → headless runs → scores → report.

Items stream through one at a time; each outcome is written to the JSONL
report as it completes (no whole-run buffering) and the final report exposes
:meth:`EvalReport.ok` for exit-code / pytest gating.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .capture import EvalPipeline, RunCapture
from .dataset import EvalDataset, EvalItem, load_eval_dataset
from .scorers import RunSucceededScorer, ScoreResult, build_scorer

if TYPE_CHECKING:
    from ..config.schema import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class ItemOutcome:
    """One item's captured run plus every scorer verdict."""

    item: EvalItem
    capture: RunCapture
    scores: list[ScoreResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True when every scorer passed."""
        return all(s.passed for s in self.scores)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form (one JSONL report line)."""
        return {
            "item_id": self.item.id,
            "input": self.item.input,
            "passed": self.passed,
            "scores": [s.to_dict() for s in self.scores],
            "text": self.capture.text,
            "tools": self.capture.tool_names(),
            "trajectory": [
                {"agent": t.agent, "name": t.name, "status": t.status, "kind": t.kind}
                for t in self.capture.tools
            ],
            "usage": self.capture.usage,
            "latency_s": round(self.capture.latency_s, 3),
            "error": self.capture.error,
        }


@dataclass
class EvalReport:
    """The full eval result: per-item outcomes plus rollups."""

    dataset: str
    outcomes: list[ItemOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every item passed every scorer."""
        return all(o.passed for o in self.outcomes)

    @property
    def failed(self) -> list[ItemOutcome]:
        """Items with at least one failing scorer."""
        return [o for o in self.outcomes if not o.passed]

    @property
    def total_cost(self) -> float:
        """Accumulated dollar cost across all items (0 when not reported)."""
        return sum(float(o.capture.usage.get("cost", 0) or 0) for o in self.outcomes)

    def summary(self) -> str:
        """Human-readable per-item table plus totals."""
        lines = [f"eval: {self.dataset}"]
        for outcome in self.outcomes:
            mark = "✓" if outcome.passed else "✗"
            failed_names = ", ".join(s.scorer for s in outcome.scores if not s.passed)
            suffix = f"  [{failed_names}]" if failed_names else ""
            cost = float(outcome.capture.usage.get("cost", 0) or 0)
            lines.append(
                f"  {mark} {outcome.item.id:<30s} "
                f"{outcome.capture.latency_s:6.1f}s  ${cost:.4f}{suffix}"
            )
            for score in outcome.scores:
                if not score.passed and score.details:
                    lines.append(f"      {score.scorer}: {score.details}")
        passed = len(self.outcomes) - len(self.failed)
        lines.append(f"  {passed}/{len(self.outcomes)} passed, total cost ${self.total_cost:.4f}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable summary."""
        return {
            "dataset": self.dataset,
            "ok": self.ok,
            "passed": len(self.outcomes) - len(self.failed),
            "total": len(self.outcomes),
            "total_cost": round(self.total_cost, 6),
            "items": [o.to_dict() for o in self.outcomes],
        }


def _build_item_scorers(
    item: EvalItem,
    dataset: EvalDataset,
    app_config: AppConfig | None,
) -> list[Any]:
    """Dataset-level scorers + the item's own, with the implicit run check first."""
    scorers: list[Any] = [RunSucceededScorer()]
    for config in [*dataset.scorers, *item.expect]:
        scorers.append(build_scorer(config, app_config))
    return scorers


async def run_eval(
    dataset: str | Path | EvalDataset,
    *,
    config: str | Path | None = None,
    item_ids: list[str] | None = None,
    max_items: int | None = None,
    output: str | Path | None = None,
    on_item: Callable[[ItemOutcome], None] | None = None,
) -> EvalReport:
    """Run a golden dataset headlessly and score every item.

    Args:
        dataset: Dataset file path (YAML/JSONL) or a loaded
            :class:`EvalDataset`.
        config: Workflow config path. Overrides the dataset's own ``config``.
        item_ids: Run only these item ids (default: all).
        max_items: Cap the number of items run.
        output: JSONL report path; each item's outcome is appended as it
            completes. Overwritten per run.
        on_item: Callback invoked after each item (progress reporting).

    Returns:
        The :class:`EvalReport` (check :attr:`EvalReport.ok`).

    Raises:
        ValueError: No workflow config was provided by either argument or
            dataset, or a scorer config is invalid.
    """
    loaded = dataset if isinstance(dataset, EvalDataset) else load_eval_dataset(dataset)
    config_path = str(config) if config is not None else loaded.config
    if not config_path:
        raise ValueError("no workflow config: pass config=... or set 'config:' in the dataset file")

    items = loaded.items
    if item_ids:
        wanted = set(item_ids)
        items = [i for i in items if i.id in wanted]
        missing = wanted - {i.id for i in items}
        if missing:
            raise ValueError(f"dataset has no item(s): {sorted(missing)}")
    if max_items is not None:
        items = items[:max_items]

    report = EvalReport(dataset=loaded.name)
    out_file = Path(output).open("w") if output is not None else None

    pipeline = EvalPipeline(config_path)
    try:
        # Fail fast on bad scorer configs before spending on any run.
        for item in items:
            _build_item_scorers(item, loaded, pipeline.app_config)

        for item in items:
            capture = await pipeline.run_item(item)
            scorers = _build_item_scorers(item, loaded, pipeline.app_config)
            outcome = ItemOutcome(item=item, capture=capture)
            for scorer in scorers:
                try:
                    outcome.scores.append(await scorer.score(item, capture))
                except Exception as exc:
                    outcome.scores.append(
                        ScoreResult(
                            getattr(scorer, "name", type(scorer).__name__),
                            False,
                            None,
                            f"scorer raised: {exc}",
                        )
                    )
            report.outcomes.append(outcome)
            if out_file is not None:
                out_file.write(json.dumps(outcome.to_dict()) + "\n")
                out_file.flush()
            if on_item is not None:
                on_item(outcome)
    finally:
        if out_file is not None:
            out_file.close()
        pipeline.close()

    return report


def assert_eval(report: EvalReport) -> None:
    """Pytest gate: raise ``AssertionError`` with the summary when items failed."""
    if not report.ok:
        raise AssertionError(f"eval failed:\n{report.summary()}")
