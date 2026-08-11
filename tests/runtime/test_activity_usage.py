"""Token usage accumulation in the activity state.

Every ``AGENT_COMPLETE`` carries the invocation's ``usage`` (from Strands
``EventLoopMetrics``). The activity adapter folds it into the completing
group (``group.usage``) and into a per-run rollup (``state.usageByRun``), so
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


def test_single_completion_records_group_and_run_usage() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    changed = reg.apply(
        "t1", _complete("g1", usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
    )

    assert changed is True
    snap = reg.snapshot("t1")
    assert snap["groups"]["g1"]["usage"] == {
        "inputTokens": 100,
        "outputTokens": 20,
        "totalTokens": 120,
    }
    assert snap["usageByRun"] == {
        "r1": {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}
    }


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
    assert snap["groups"]["g1"]["usage"] == {
        "inputTokens": 130,
        "outputTokens": 25,
        "totalTokens": 155,
    }
    assert snap["usageByRun"]["r1"] == {"inputTokens": 130, "outputTokens": 25, "totalTokens": 155}


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
        "r1": {"inputTokens": 30, "outputTokens": 3, "totalTokens": 33},
        "r2": {"inputTokens": 5, "outputTokens": 3, "totalTokens": 8},
    }


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
    assert snap["usageByRun"] == {"r1": {"inputTokens": 7, "outputTokens": 2, "totalTokens": 9}}
    assert snap["groups"] == {}


def test_completion_without_usage_changes_nothing_extra() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    reg.apply("t1", _complete("g1", structured_output={"a": 1}, output_schema_name="Thing"))

    snap = reg.snapshot("t1")
    assert "usage" not in snap["groups"]["g1"]
    assert "usageByRun" not in snap
    assert snap["groups"]["g1"]["structuredOutput"] == {"a": 1}


def test_missing_usage_fields_default_to_zero() -> None:
    reg = ActivityRegistry()
    reg.apply("t1", _start("g1"))
    reg.apply("t1", _complete("g1", usage={"input_tokens": 4}))

    snap = reg.snapshot("t1")
    assert snap["groups"]["g1"]["usage"] == {"inputTokens": 4, "outputTokens": 0, "totalTokens": 0}
