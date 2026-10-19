"""End-to-end proof that the run's token rollup counts every agent.

The per-run rollup (``usageByRun``) must include the entry/manager agent —
whose event stream is excluded from group rendering — alongside every
delegated worker, so a UI can show a true full-run total.
"""

from __future__ import annotations

from pathlib import Path

from kaboo_workflows.testing import run_pipeline

CONFIGS = Path(__file__).parent / "configs"


async def test_delegate_rollup_counts_manager_and_workers() -> None:
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go", run_id="r1")
    assert r.finished() and not r.errored()

    assert "r1" in r.usage_by_run
    run_total = r.usage_by_run["r1"]["totalTokens"]

    def tokens(owner: bool) -> int:
        groups = [g for g in r.groups.values() if bool(g["inlineChatOwner"]) is owner]
        return sum(g.get("usage", {}).get("totalTokens", 0) for g in groups)

    workers, manager = tokens(False), tokens(True)
    assert workers > 0 and manager > 0
    # The manager's usage sits on its own inline group, never drawn as a card,
    # so the run total is the manager's and the workers' together.
    assert run_total == workers + manager


async def test_plain_entry_rollup_records_usage() -> None:
    r = await run_pipeline(str(CONFIGS / "plain.yaml"), "hi", run_id="r1")
    assert r.finished() and not r.errored()
    assert r.usage_by_run.get("r1", {}).get("totalTokens", 0) > 0
