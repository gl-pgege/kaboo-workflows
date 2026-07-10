"""Transcript well-formedness guards in the AG-UI adapter.

These protect the fresh-run path, which replays the client transcript 1:1 into
strands and calls ``stream_async(None)``. A dangling or duplicate tool block
produces a native history the model rejects — a silent, event-less hang — so the
adapter detects imbalance (to fail fast) and closes abandoned calls (on a
superseded interrupt) rather than emitting a malformed stream.
"""

from __future__ import annotations

from types import SimpleNamespace

from ag_ui.core import AssistantMessage, FunctionCall, ToolCall, ToolMessage, UserMessage

from kaboo_workflows._context import HistoryExchange
from kaboo_workflows.adapters.agui import (
    _close_abandoned_tool_calls,
    _collapse_duplicate_tool_results,
    _enrich_history_snapshot,
    _find_transcript_imbalance,
    normalize_client_transcript,
)


def _assistant_call(msg_id: str, tc_id: str, name: str = "research_team") -> AssistantMessage:
    return AssistantMessage(
        id=msg_id,
        tool_calls=[ToolCall(id=tc_id, function=FunctionCall(name=name, arguments="{}"))],
    )


def _tool_result(msg_id: str, tc_id: str, content: str = "result") -> ToolMessage:
    return ToolMessage(id=msg_id, content=content, tool_call_id=tc_id)


# ── _find_transcript_imbalance ───────────────────────────────────────────────


def test_balanced_transcript_is_accepted() -> None:
    messages = [
        UserMessage(id="u1", content="hi"),
        _assistant_call("a1", "tc1"),
        _tool_result("t1", "tc1"),
        UserMessage(id="u2", content="thanks"),
    ]

    assert _find_transcript_imbalance(messages) is None


def test_dangling_tool_call_is_flagged() -> None:
    messages = [_assistant_call("a1", "tc1"), UserMessage(id="u2", content="next")]

    problem = _find_transcript_imbalance(messages)

    assert problem is not None
    assert "missing a result" in problem
    assert "tc1" in problem


def test_duplicate_result_is_flagged() -> None:
    messages = [
        _assistant_call("a1", "tc1"),
        _tool_result("t1", "tc1"),
        _tool_result("t2", "tc1"),
    ]

    problem = _find_transcript_imbalance(messages)

    assert problem is not None
    assert "duplicate results" in problem
    assert "tc1" in problem


# ── _close_abandoned_tool_calls ──────────────────────────────────────────────


def test_dangling_call_is_closed_in_place() -> None:
    data = SimpleNamespace(
        messages=[_assistant_call("a1", "tc1"), UserMessage(id="u2", content="new turn")]
    )

    closed = _close_abandoned_tool_calls(data)

    assert closed == ["tc1"]
    roles = [m.role for m in data.messages]
    assert roles == ["assistant", "tool", "user"]
    inserted = data.messages[1]
    assert inserted.tool_call_id == "tc1"
    # The repaired transcript is now well-formed for replay.
    assert _find_transcript_imbalance(data.messages) is None


def test_balanced_transcript_is_left_unchanged() -> None:
    original = [_assistant_call("a1", "tc1"), _tool_result("t1", "tc1")]
    data = SimpleNamespace(messages=list(original))

    closed = _close_abandoned_tool_calls(data)

    assert closed == []
    assert data.messages == original


# ── _collapse_duplicate_tool_results ─────────────────────────────────────────


def test_collapses_multi_interrupt_delegate_results_to_the_last() -> None:
    # A delegate call whose nested sub-agent asked the user AND requested tool
    # approval: two interrupt-resolution results, then the genuine final result.
    data = SimpleNamespace(
        messages=[
            UserMessage(id="u1", content="research the market"),
            _assistant_call("a1", "tc1"),
            _tool_result("t1", "tc1", content="ask_user answers"),
            _tool_result("t2", "tc1", content="approval"),
            _tool_result("t3", "tc1", content="final delegate output"),
            UserMessage(id="u2", content="now do the same for X"),
        ]
    )

    dropped = _collapse_duplicate_tool_results(data)

    assert dropped == {"tc1": 2}
    roles = [m.role for m in data.messages]
    assert roles == ["user", "assistant", "tool", "user"]
    # The genuine (last) result is the one kept.
    kept = next(m for m in data.messages if m.role == "tool")
    assert kept.id == "t3"
    # The repaired transcript now passes the guard.
    assert _find_transcript_imbalance(data.messages) is None


def test_collapse_leaves_a_balanced_transcript_untouched() -> None:
    original = [
        _assistant_call("a1", "tc1"),
        _tool_result("t1", "tc1"),
        _assistant_call("a2", "tc2"),
        _tool_result("t2", "tc2"),
    ]
    data = SimpleNamespace(messages=list(original))

    dropped = _collapse_duplicate_tool_results(data)

    assert dropped == {}
    assert data.messages == original


def test_collapse_is_per_tool_call_id() -> None:
    data = SimpleNamespace(
        messages=[
            _assistant_call("a1", "tc1"),
            _tool_result("t1", "tc1"),
            _tool_result("t2", "tc1"),
            _assistant_call("a2", "tc2"),
            _tool_result("t3", "tc2"),
        ]
    )

    dropped = _collapse_duplicate_tool_results(data)

    assert dropped == {"tc1": 1}
    result_ids = [m.tool_call_id for m in data.messages if m.role == "tool"]
    assert result_ids == ["tc1", "tc2"]


# ── normalize_client_transcript (the single boundary) ───────────────────────


def test_normalize_repairs_dupes_and_reports_clean_residual() -> None:
    data = SimpleNamespace(
        messages=[
            _assistant_call("a1", "tc1"),
            _tool_result("t1", "tc1", content="ask_user"),
            _tool_result("t2", "tc1", content="final"),
        ]
    )

    repairs = normalize_client_transcript(data, close_dangling=False)

    assert repairs.collapsed == {"tc1": 1}
    assert repairs.backfill_ids == []
    assert repairs.residual is None
    assert _find_transcript_imbalance(data.messages) is None


def test_normalize_closes_dangling_only_when_superseded() -> None:
    def _data() -> SimpleNamespace:
        return SimpleNamespace(
            messages=[_assistant_call("a1", "tc1"), UserMessage(id="u2", content="new turn")]
        )

    # Not superseded: a dangling call is foreign — left for the residual to refuse.
    kept = normalize_client_transcript(_data(), close_dangling=False)
    assert kept.backfill_ids == []
    assert kept.residual is not None
    assert "missing a result" in kept.residual

    # Superseded: the abandoned call is closed and reported for frontend backfill.
    repaired = normalize_client_transcript(_data(), close_dangling=True)
    assert repaired.backfill_ids == ["tc1"]
    assert repaired.residual is None


def test_normalize_leaves_a_clean_transcript_untouched() -> None:
    original = [_assistant_call("a1", "tc1"), _tool_result("t1", "tc1")]
    data = SimpleNamespace(messages=list(original))

    repairs = normalize_client_transcript(data, close_dangling=True)

    assert repairs == ([], {}, None)
    assert data.messages == original


# ── _enrich_history_snapshot ─────────────────────────────────────────────────


def test_snapshot_gets_merged_kaboo_history() -> None:
    exchange = HistoryExchange(
        inbound={"team": [{"role": "user", "content": [{"text": "old"}]}]},
        outbound={"team": [{"role": "user", "content": [{"text": "new"}]}]},
    )
    event = SimpleNamespace(snapshot={"other": 1})

    _enrich_history_snapshot(event, exchange)

    # Outbound (this run) wins over inbound for the same bucket.
    assert event.snapshot["kaboo_history"]["team"] == [
        {"role": "user", "content": [{"text": "new"}]}
    ]
    assert event.snapshot["other"] == 1


def test_snapshot_enrichment_ignores_non_dict_snapshot() -> None:
    event = SimpleNamespace(snapshot=None)

    # Must not raise.
    _enrich_history_snapshot(event, HistoryExchange(outbound={"team": [{"x": 1}]}))

    assert event.snapshot is None
