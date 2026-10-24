"""State effects write tool results into shared state; workspace views show it to agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from ag_ui.core import EventType as AGUIEventType

from kaboo_workflows.config.schema import StateEffectDef
from kaboo_workflows.testing import Pipeline, ScriptedModel

CONFIGS = Path(__file__).parent / "configs"


class RecordingModel(ScriptedModel):
    """A scripted model that keeps the text of every request it receives."""

    seen: ClassVar[dict[str, list[str]]] = {}

    async def stream(self, messages: Any, *args: Any, **kwargs: Any) -> Any:
        text = " ".join(
            block.get("text", "") for message in messages for block in message.get("content", [])
        )
        RecordingModel.seen.setdefault(str(self.get_config().get("model_id")), []).append(text)
        async for event in super().stream(messages, *args, **kwargs):
            yield event


async def test_a_tool_result_reaches_the_client_and_the_agents_next_model_call() -> None:
    RecordingModel.seen.clear()
    r = await Pipeline(str(CONFIGS / "state_effects.yaml")).turn("go", thread_id="w1", run_id="r1")

    assert r.finished() and not r.errored()
    deltas = r.of_type(AGUIEventType.STATE_DELTA)
    assert any(op.path == "/kaboo_workspace/queries/q_7" for delta in deltas for op in delta.delta)
    assert r.state()["kaboo_workspace"]["queries"] == {
        "q_7": {"title": "Churn by region", "by": "worker"}
    }
    first, after = RecordingModel.seen["coordinator"][0], RecordingModel.seen["coordinator"][-1]
    assert "q_7" not in first
    assert '/kaboo_workspace/queries: {"q_7":{"title":"Churn by region","by":"worker"}}' in after


async def test_an_agent_sees_only_the_parts_of_the_workspace_it_names() -> None:
    RecordingModel.seen.clear()
    await Pipeline(str(CONFIGS / "state_effects.yaml")).turn(
        "go", thread_id="w2", run_id="r1", state={"kaboo_workspace": {"queries": {}, "secret": 1}}
    )
    assert all("secret" not in text for text in RecordingModel.seen["coordinator"])
    assert all("The workspace" not in text for text in RecordingModel.seen["worker"])


async def test_a_path_effect_writes_the_result_whole() -> None:
    raw = (
        (CONFIGS / "state_effects.yaml")
        .read_text()
        .replace("mapper: tests.fakes.workspace_tools:saved", "path: /last_saved")
    )
    r = await Pipeline(raw).turn("go", thread_id="w3", run_id="r1")
    assert r.state()["last_saved"] == {"id": "q_7", "title": "Churn by region"}


@pytest.mark.parametrize("raw", [{"tool": "t"}, {"tool": "t", "mapper": "m:f", "path": "/x"}])
def test_an_effect_takes_a_mapper_or_a_path(raw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        StateEffectDef.model_validate(raw)
