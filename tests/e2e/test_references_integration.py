"""Integration: references flow through a real strands run to the model.

Drives a real ``Agent`` with a recording model and the ReferenceHook wired as
``build_agent_from_def`` would, then asserts the manifest reaches the model's
system prompt and inline media reaches its messages — the observable end of the
parse -> context -> hook pipeline.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from strands import Agent

from kaboo_workflows._context import Reference, set_references
from kaboo_workflows.hooks import ReferenceHook
from tests.fakes import FakeModel


class RecordingModel(FakeModel):
    """FakeModel that records every (system_prompt, messages) it is called with."""

    def __init__(self) -> None:
        super().__init__(text_chunks=["ok"])
        self.system_prompts: list[str | None] = []
        self.message_batches: list[Any] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):  # type: ignore[override]
        self.system_prompts.append(system_prompt)
        self.message_batches.append(messages)
        async for chunk in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield chunk


@pytest.fixture(autouse=True)
def _isolate_refs() -> Iterator[None]:
    set_references(None)
    yield
    set_references(None)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def test_reference_manifest_reaches_the_model_in_a_real_run():
    model = RecordingModel()
    agent = Agent(
        model=model,
        system_prompt="Base prompt.",
        hooks=[ReferenceHook(enabled=True, inline=False, tool_enabled=True)],
    )
    set_references(
        [
            Reference(
                kind="table", id="t1", name="orders", transport="object", meta={"schema": "public"}
            )
        ]
    )

    agent("What's in the orders table?")

    assert any("Referenced items" in (sp or "") for sp in model.system_prompts)
    assert any("orders" in (sp or "") and "t1" in (sp or "") for sp in model.system_prompts)


def test_inline_media_reaches_the_model_messages_in_a_real_run():
    model = RecordingModel()
    agent = Agent(
        model=model,
        system_prompt="Base.",
        hooks=[ReferenceHook(enabled=True, inline=True, tool_enabled=True)],
    )
    set_references(
        [
            Reference(
                kind="file",
                id="img_1",
                name="a.png",
                transport="attachment",
                mime_type="image/png",
                source="data",
                value=_b64(b"\x89PNG\r\n"),
            )
        ]
    )

    agent("Describe the image.")

    first_batch = model.message_batches[0]
    blocks = [b for msg in first_batch for b in msg.get("content", []) if isinstance(b, dict)]
    assert any("image" in b for b in blocks)
