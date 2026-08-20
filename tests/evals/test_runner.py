"""End-to-end eval runner against scripted-model workflows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kaboo_workflows.evals import assert_eval, load_eval_dataset, run_eval

CONFIGS = Path(__file__).parent.parent / "e2e" / "configs"


def _dataset(tmp_path: Path, body: str) -> Path:
    file = tmp_path / "golden.yaml"
    file.write_text(body)
    return file


async def test_plain_workflow_passes_deterministic_checks(tmp_path):
    dataset = _dataset(
        tmp_path,
        f"""
name: plain-golden
config: {CONFIGS / "plain.yaml"}
items:
  - id: greet
    input: "hi"
    expect:
      - type: contains
        value: "plain agent"
""",
    )
    out = tmp_path / "results.jsonl"
    report = await run_eval(dataset, output=out)

    assert report.ok
    assert report.outcomes[0].capture.text == "Hello from the plain agent."
    assert report.outcomes[0].capture.usage.get("totalTokens", 0) > 0
    assert_eval(report)

    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["item_id"] == "greet"
    assert lines[0]["passed"] is True
    assert all({"agent", "name", "status", "kind"} <= set(t) for t in lines[0]["trajectory"])


async def test_entry_agent_tool_calls_are_captured(tmp_path):
    config = tmp_path / "entry_tools.yaml"
    config.write_text(
        """
agents:
  assistant:
    stream: {title: "Assistant"}
    tools: ["tests.fakes.gated_tools:submit_report"]
    model:
      provider: tests.fakes.scripted_model:ScriptedModel
      model_id: scripted
      params:
        script:
          - {tool: submit_report, input: {title: "Q3"}}
        final_text: "report filed"

entry: assistant
"""
    )
    dataset = _dataset(
        tmp_path,
        f"""
name: entry-tools-golden
config: {config}
items:
  - id: file-report
    input: "file the report"
    expect:
      - type: tool_called
        tool: submit_report
      - type: contains
        value: "report filed"
""",
    )
    report = await run_eval(dataset)
    assert report.ok, report.summary()
    assert "submit_report" in report.outcomes[0].capture.tool_names()


async def test_item_state_references_reach_the_run(tmp_path, monkeypatch):
    import kaboo_workflows.evals.capture as capture_mod
    from kaboo_workflows.evals.capture import EvalPipeline
    from kaboo_workflows.evals.dataset import EvalItem

    seen: list[list] = []
    real = capture_mod.set_references

    def spy(refs):
        seen.append(refs)
        real(refs)

    monkeypatch.setattr(capture_mod, "set_references", spy)

    pipeline = EvalPipeline(str(CONFIGS / "plain.yaml"))
    try:
        item = EvalItem(
            id="refs",
            input="hi",
            state={
                "kaboo_references": [
                    {
                        "kind": "database",
                        "id": "db-1",
                        "name": "cust-db",
                        "meta": {"databaseId": "db-1", "databaseName": "cust-db"},
                    }
                ]
            },
        )
        capture = await pipeline.run_item(item)
    finally:
        pipeline.close()

    assert capture.error is None
    refs = seen[-1]
    assert len(refs) == 1
    assert refs[0].kind == "database"
    assert refs[0].id == "db-1"
    assert refs[0].meta["databaseName"] == "cust-db"


async def test_delegate_workflow_captures_tool_trajectory(tmp_path):
    dataset = _dataset(
        tmp_path,
        f"""
name: delegate-golden
config: {CONFIGS / "delegate.yaml"}
scorers:
  - type: budget
    max_latency_s: 120
items:
  - id: teamwork
    input: "go"
    expect:
      - type: tool_called
        tool: worker_a
      - type: trajectory
        tools: [worker_a, worker_b]
      - type: contains
        value: "combined"
""",
    )
    report = await run_eval(dataset)
    assert report.ok, report.summary()
    tools = report.outcomes[0].capture.tool_names()
    assert "worker_a" in tools and "worker_b" in tools


async def test_failing_expectation_fails_the_report(tmp_path):
    dataset = _dataset(
        tmp_path,
        f"""
config: {CONFIGS / "plain.yaml"}
items:
  - id: wrong
    input: "hi"
    expect:
      - type: contains
        value: "text that will never appear"
""",
    )
    report = await run_eval(dataset)
    assert not report.ok
    assert [o.item.id for o in report.failed] == ["wrong"]
    with pytest.raises(AssertionError, match="eval failed"):
        assert_eval(report)


async def test_item_filter_and_max_items(tmp_path):
    dataset = _dataset(
        tmp_path,
        f"""
config: {CONFIGS / "plain.yaml"}
items:
  - {{id: one, input: "a"}}
  - {{id: two, input: "b"}}
  - {{id: three, input: "c"}}
""",
    )
    report = await run_eval(dataset, item_ids=["two"])
    assert [o.item.id for o in report.outcomes] == ["two"]

    report = await run_eval(dataset, max_items=2)
    assert [o.item.id for o in report.outcomes] == ["one", "two"]

    with pytest.raises(ValueError, match="no item"):
        await run_eval(dataset, item_ids=["missing"])


async def test_bad_scorer_config_fails_before_any_run(tmp_path):
    dataset = _dataset(
        tmp_path,
        f"""
config: {CONFIGS / "plain.yaml"}
items:
  - id: x
    input: "hi"
    expect:
      - type: made_up_scorer
""",
    )
    with pytest.raises(ValueError, match="unknown scorer type"):
        await run_eval(dataset)


async def test_eval_pipeline_initializes_telemetry(tmp_path, monkeypatch):
    called = {}

    def fake_init(cfg):
        called["cfg"] = cfg
        return False

    monkeypatch.setattr("kaboo_workflows.telemetry.init_telemetry", fake_init)
    dataset = _dataset(
        tmp_path,
        f"""
config: {CONFIGS / "plain.yaml"}
items:
  - id: greet
    input: "hi"
""",
    )
    report = await run_eval(dataset)
    assert report.ok
    assert "cfg" in called


async def test_config_argument_overrides_dataset(tmp_path):
    dataset = _dataset(
        tmp_path,
        """
items:
  - id: greet
    input: "hi"
""",
    )
    loaded = load_eval_dataset(dataset)
    assert loaded.config is None
    with pytest.raises(ValueError, match="no workflow config"):
        await run_eval(dataset)
    report = await run_eval(dataset, config=CONFIGS / "plain.yaml")
    assert report.ok
