"""ToolAttachmentsHook — media a tool returns as data reaches the model as media.

Drives a real ``Agent`` whose scripted model calls a tool returning
``{"attachments": [...]}`` and records what the next model call receives, plus
direct checks of :func:`unpack_attachments` and the build-time wiring.
"""

from __future__ import annotations

import base64
import copy
import json
from typing import Any

from strands import Agent, tool
from strands.models.model import Model

from kaboo_workflows.config.resolvers.agents import build_agent_from_def, get_agent_hook_providers
from kaboo_workflows.hooks import ToolAttachmentsHook
from kaboo_workflows.hooks.tool_attachments_hook import unpack_attachments
from tests.factories import agent_def
from tests.fakes import FakeModel

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
DATA = base64.b64encode(PNG).decode()


def _result(**body: Any) -> list[dict[str, Any]]:
    return [{"text": json.dumps(body)}]


def test_unpacks_attachments_into_named_media_after_the_text():
    content = unpack_attachments(
        _result(
            success=True, attachments=[{"name": "Rows 1-7", "mime_type": "image/png", "data": DATA}]
        )
    )

    assert content is not None
    assert json.loads(content[0]["text"]) == {"success": True}
    assert content[1] == {"text": "Rows 1-7"}
    assert content[2] == {"image": {"format": "png", "source": {"bytes": PNG}}}


def test_leaves_a_result_without_attachments_alone():
    assert unpack_attachments([{"text": "not json"}]) is None
    assert unpack_attachments(_result(success=True)) is None


def test_names_what_it_had_to_drop():
    content = unpack_attachments(
        _result(
            attachments=[
                {"name": "broken", "mime_type": "image/png", "data": "@@@"},
                {"name": "sound", "mime_type": "audio/mpeg", "data": DATA},
            ]
        )
    )

    assert content is not None
    assert [b["text"] for b in content[1:]] == [
        "broken: dropped, its data is not valid base64.",
        "sound: dropped, audio/mpeg is not a supported media type.",
    ]


class _CallThenRecord(Model):
    """Calls ``snapshot`` on the first turn, then records the messages it is sent."""

    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {}

    async def structured_output(self, *args: Any, **kwargs: Any):  # ty: ignore[invalid-method-override]
        raise NotImplementedError
        yield

    async def stream(self, messages: Any, *args: Any, **kwargs: Any):  # ty: ignore[invalid-method-override]
        self.seen.append(copy.deepcopy(messages))
        yield {"messageStart": {"role": "assistant"}}
        if len(self.seen) == 1:
            yield {
                "contentBlockStart": {"start": {"toolUse": {"toolUseId": "t1", "name": "snapshot"}}}
            }
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": "{}"}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
            return
        yield {"contentBlockDelta": {"delta": {"text": "seen"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


@tool
def snapshot() -> dict:
    """Return a picture of the dashboard."""
    body = {
        "summary": "1 widget",
        "attachments": [{"name": "Rows 1-2", "mime_type": "image/png", "data": DATA}],
    }
    return {"status": "success", "content": [{"text": json.dumps(body)}]}


async def test_the_model_receives_the_image_in_the_tool_result():
    model = _CallThenRecord()
    agent = Agent(
        model=model, tools=[snapshot], hooks=[ToolAttachmentsHook()], callback_handler=None
    )

    await agent.invoke_async("Look at it.")

    result = next(b["toolResult"] for b in model.seen[1][-1]["content"] if "toolResult" in b)
    assert json.loads(result["content"][0]["text"]) == {"summary": "1 widget"}
    assert result["content"][1:] == [
        {"text": "Rows 1-2"},
        {"image": {"format": "png", "source": {"bytes": PNG}}},
    ]


def test_every_built_agent_unpacks_tool_attachments():
    agent = build_agent_from_def("a", agent_def(model="fast"), {"fast": FakeModel()}, {})

    assert any(isinstance(h, ToolAttachmentsHook) for h in get_agent_hook_providers(agent))
