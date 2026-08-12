"""Token usage accumulation in the activity state.

Every ``AGENT_COMPLETE`` carries the invocation's ``usage`` (from Strands
``EventLoopMetrics``, plus the per-invocation dollar cost captured by the
model adapter). The activity adapter folds it into the completing group
(``group.usage``) and into a per-run rollup (``state.usageByRun``), so
consumers can show per-agent counts and O(1) full-run totals — both of which
persist and replay because they ride ``ACTIVITY_SNAPSHOT`` events verbatim.
"""

from __future__ import annotations

from typing import Any

from kaboo_workflows.adapters._activity import ActivityRegistry
from kaboo_workflows.types import EventType, StreamEvent


def _start(group: str, run_id: str = "r1") -> StreamEvent:
    return StreamEvent(
        type=EventType.STREAM_GROUP_START,
        agent_name="analyst",
        data={"stream_group": group, "stream_title": "Analysis", "run_id": run_id},
    )


def _complete(
    group: str,
    run_id: str = "r1",
    usage: dict[str, Any] | None = None,
    **extra: Any,
) -> StreamEvent:
    data: dict[str, Any] = {"stream_group": group, "run_id": run_id, **extra}
    if usage is not None:
        data["usage"] = usage
    return StreamEvent(type=EventType.AGENT_COMPLETE, agent_name="analyst", data=data)


def _bucket(
    inputTokens: int = 0,
    outputTokens: int = 0,
    totalTokens: int = 0,
    cacheReadInputTokens: int = 0,
    cacheWriteInputTokens: int = 0,
    cost: float = 0.0,
) -> dict[str, Any]:
    return {
        "inputTokens": inputTokens,
        "outputTokens": outputTokens,
        "totalTokens": totalTokens,
        "cacheReadInputTokens": cacheReadInputTokens,
        "cacheWriteInputTokens": cacheWriteInputTokens,
        "cost": cost,
    }


def test_single_completion_records_group_and_run_usage() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    changed = reg.apply(
        "t1", _complete("g1", usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    )

    assert changed is True
    snap = reg.snapshot("t1")
    assert snap["groups"]["g1"]["usage"] == _bucket(100, 20, 120)
    assert snap["usageByRun"] == {"r1": _bucket(100, 20, 120)}


def test_multiple_completions_accumulate_not_overwrite() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    reg.apply(
        "t1", _complete("g1", usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    )
    # Interrupt resume or delegate re-entry: the same agent completes again.
    reg.apply(
        "t1", _complete("g1", usage={"input_tokens": 30, "output_tokens": 5, "total_tokens": 35})
    )

    snap = reg.snapshot("t1")
    assert snap["groups"]["g1"]["usage"] == _bucket(130, 25, 155)
    assert snap["usageByRun"]["r1"] == _bucket(130, 25, 155)


def test_usage_rolls_up_per_run_across_groups() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1", run_id="r1"))
    reg.apply("t1", _start("g2", run_id="r1"))
    reg.apply(
        "t1",
        _complete(
            "g1", run_id="r1", usage={"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
        ),
    )
    reg.apply(
        "t1",
        _complete(
            "g2", run_id="r1", usage={"input_tokens": 20, "output_tokens": 2, "total_tokens": 22}
        ),
    )
    # A later run (e.g. resume) accumulates under its own run id.
    reg.apply("t1", _start("g3", run_id="r2"))
    reg.apply(
        "t1",
        _complete(
            "g3", run_id="r2", usage={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8}
        ),
    )

    snap = reg.snapshot("t1")
    assert snap["usageByRun"] == {
        "r1": _bucket(30, 3, 33),
        "r2": _bucket(5, 3, 8),
    }


def test_cache_and_cost_fields_accumulate() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    reg.apply(
        "t1",
        _complete(
            "g1",
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "cache_read_input_tokens": 80,
                "cache_write_input_tokens": 15,
                "cost": 0.012,
            },
        ),
    )
    reg.apply(
        "t1",
        _complete(
            "g1",
            usage={
                "input_tokens": 50,
                "output_tokens": 10,
                "total_tokens": 60,
                "cache_read_input_tokens": 45,
                "cost": 0.003,
            },
        ),
    )

    snap = reg.snapshot("t1")
    usage = snap["groups"]["g1"]["usage"]
    assert usage["cacheReadInputTokens"] == 125
    assert usage["cacheWriteInputTokens"] == 15
    assert abs(usage["cost"] - 0.015) < 1e-9
    run = snap["usageByRun"]["r1"]
    assert run["cacheReadInputTokens"] == 125
    assert abs(run["cost"] - 0.015) < 1e-9


def test_groupless_usage_still_counts_toward_the_run() -> None:
    reg = ActivityRegistry()
    event = StreamEvent(
        type=EventType.AGENT_COMPLETE,
        agent_name="orchestrator",
        data={"run_id": "r1", "usage": {"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}},
    )

    changed = reg.apply("t1", event)

    assert changed is True
    snap = reg.snapshot("t1")
    assert snap["usageByRun"] == {"r1": _bucket(7, 2, 9)}
    assert snap["groups"] == {}


def test_completion_without_usage_changes_nothing_extra() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    reg.apply("t1", _complete("g1", structured_output={"a": 1}, output_schema_name="Thing"))

    snap = reg.snapshot("t1")
    assert "usage" not in snap["groups"]["g1"]
    assert "usageByRun" not in snap
    assert snap["groups"]["g1"]["structuredOutput"] == {"a": 1}


def test_apply_usage_records_rollup_without_touching_groups() -> None:
    """The entry/manager agent's stream is excluded from group rendering, but
    its completions must still count toward the run total via apply_usage."""
    reg = ActivityRegistry()
    changed = reg.apply_usage(
        "t1",
        _complete("main", usage={"input_tokens": 500, "output_tokens": 40, "total_tokens": 540}),
    )
    assert changed is True
    snap = reg.snapshot("t1")
    assert "main" not in snap["groups"]
    assert snap["usageByRun"]["r1"]["totalTokens"] == 540


def test_apply_usage_ignores_non_complete_and_usage_free_events() -> None:
    reg = ActivityRegistry()
    assert reg.apply_usage("t1", _start("main")) is False
    assert reg.apply_usage("t1", _complete("main")) is False
    assert "usageByRun" not in reg.snapshot("t1")


def test_missing_usage_fields_default_to_zero() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    reg.apply("t1", _complete("g1", usage={"input_tokens": 4}))

    snap = reg.snapshot("t1")
    assert snap["groups"]["g1"]["usage"] == _bucket(4)


def test_pre_existing_bucket_without_new_keys_upgrades_in_place() -> None:
    """Buckets restored from a pre-0.16 snapshot lack the cache/cost keys;
    accumulation must tolerate and backfill them."""
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    reg.apply("t1", _complete("g1", usage={"input_tokens": 1}))
    # Simulate an old-format bucket by stripping the new keys from live state.
    state = reg._states["t1"]
    for bucket in (state["groups"]["g1"]["usage"], state["usageByRun"]["r1"]):
        for key in ("cacheReadInputTokens", "cacheWriteInputTokens", "cost"):
            bucket.pop(key, None)
    reg.apply(
        "t1", _complete("g1", usage={"input_tokens": 2, "cache_read_input_tokens": 2, "cost": 0.1})
    )

    snap = reg.snapshot("t1")
    assert snap["groups"]["g1"]["usage"]["inputTokens"] == 3
    assert snap["groups"]["g1"]["usage"]["cacheReadInputTokens"] == 2
    assert abs(snap["groups"]["g1"]["usage"]["cost"] - 0.1) < 1e-9
