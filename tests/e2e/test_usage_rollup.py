"""End-to-end proof that a run's token total counts every agent.

``RUN_FINISHED.usage`` must include the entry agent alongside every delegated
worker, so a UI can show a true full-run total; each worker also reports its
own usage when it finishes.
"""

from __future__ import annotations

from pathlib import Path

from ag_ui.core import EventType as AGUIEventType

from kaboo_workflows.testing import run_pipeline

CONFIGS = Path(__file__).parent / "configs"


async def test_delegate_run_total_counts_manager_and_workers() -> None:
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go", run_id="r1")
    assert r.finished() and not r.errored()

    run_total = r.usage_by_run["r1"]["totalTokens"]
    workers = sum((g.get("usage") or {}).get("totalTokens", 0) for g in r.groups.values())
    assert workers > 0
    # The manager's own tokens are what the run total holds beyond the workers'.
    assert run_total > workers


async def test_a_worker_reports_its_usage_when_it_finishes() -> None:
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go", run_id="r1")
    finished = r.of_type(AGUIEventType.SUBAGENT_FINISHED)
    assert finished
    assert all((e.metadata or {}).get("usage", {}).get("totalTokens", 0) > 0 for e in finished)


async def test_plain_entry_records_usage() -> None:
    r = await run_pipeline(str(CONFIGS / "plain.yaml"), "hi", run_id="r1")
    assert r.finished() and not r.errored()
    assert r.usage_by_run.get("r1", {}).get("totalTokens", 0) > 0


async def test_the_run_and_each_worker_name_their_trace(monkeypatch) -> None:
    monkeypatch.setattr(
        "kaboo_workflows.hooks.event_publisher.current_trace_id", lambda **_: "f" * 32
    )
    r = await run_pipeline(str(CONFIGS / "delegate.yaml"), "go", run_id="r1")
    [finished] = r.of_type(AGUIEventType.RUN_FINISHED)
    assert (finished.metadata or {}).get("traceId") == "f" * 32
    assert r.groups and all(group["traceId"] == "f" * 32 for group in r.groups.values())
