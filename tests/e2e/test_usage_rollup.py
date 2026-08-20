"""End-to-end proof that the run's token rollup counts every agent.

The per-run rollup (``usageByRun``) must include the entry/manager agent —
whose event stream is excluded from group rendering — alongside every
delegated worker, so a UI can show a true full-run total.
"""

from __future__ import annotations

from pathlib import Path

from .harness import run_pipeline

CONFIGS = Path(__file__).parent / "configs"


async def test_delegate_rollup_counts_manager_and_workers() -> None:
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go", run_id="r1")
    assert r.finished() and not r.errored()

    assert "r1" in r.usage_by_run
    run_total = r.usage_by_run["r1"]["totalTokens"]
    worker_total = sum(
        g.get("usage", {}).get("totalTokens", 0) for g in r.groups.values()
    )
    assert worker_total > 0  # workers report per-group usage
    # The manager (entry) has no rendered group but its completions must still
    # count, so the run total strictly exceeds the sum of the group chips.
    assert run_total > worker_total


async def test_plain_entry_rollup_records_usage() -> None:
    r = await run_pipeline(str(CONFIGS / "plain.yaml"), "hi", run_id="r1")
    assert r.finished() and not r.errored()
    assert r.usage_by_run.get("r1", {}).get("totalTokens", 0) > 0
